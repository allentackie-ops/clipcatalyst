// Browser-engine speaker features: per-frame MFCCs from the decoded PCM, and
// per-segment voice embeddings built from those frames.
//
// The split matters for ordering: the transcription worker takes ownership of
// the PCM buffer (transferred, not copied), but speech segments only exist
// once the transcript is back. So `computeMfccFrames` runs over the whole
// recording BEFORE the transfer, and `segmentEmbeddingsFromFrames` slices the
// stashed frames into per-segment embeddings afterwards.
//
// This module implements the SPEAKERS.md embedding recipe only — every
// judgement (clustering, one-vs-many, labeling) lives in diarize.ts. Pure and
// deterministic: no DOM, no randomness. `computeMfccFrames` is async solely
// to yield to the event loop between adaptively sized chunks (~12 ms of
// synchronous math each, via a reused MessageChannel) so long videos never
// freeze the UI thread.

import type { SpeechSegment } from "./diarize";

export type MfccFrames = {
  /**
   * numFrames × dims, row-major. Dim 0 = frame RMS, dim 1 = 300–3000 Hz
   * spectral flatness (the speech-likeness gate), dims 2..13 = c1..c12.
   */
  frames: Float32Array;
  /** Values per frame (RMS + flatness + 12 cepstral coefficients). */
  dims: number;
  /** Seconds between consecutive frame starts. */
  hopSeconds: number;
};

// --- Recipe constants (SPEAKERS.md — keep in step with speaker_embed.py) ----

const SAMPLE_RATE = 16000;
/** 25 ms analysis window. */
const FRAME_SIZE = 400;
/** 20 ms hop. */
const HOP_SIZE = 320;
const FFT_SIZE = 512;
const FFT_BITS = 9; // 2^9 = 512
const MEL_FILTER_COUNT = 24;
const MEL_LO_HZ = 80;
const MEL_HI_HZ = 7600;
/** Keep c1..c12; c0 is mic distance, not identity. */
const CEPSTRA = 12;
const LOG_FLOOR = 1e-10;
/** Frames quieter than this carry no voice worth averaging. */
const VOICED_RMS = 0.008;
/** Speech-likeness gate band (~300–3000 Hz) as inclusive FFT-bin indices. */
const FLATNESS_LO_BIN = Math.ceil((300 * FFT_SIZE) / SAMPLE_RATE); // bin 10
const FLATNESS_HI_BIN = Math.floor((3000 * FFT_SIZE) / SAMPLE_RATE); // bin 96
/**
 * Frames whose 300–3000 Hz spectral flatness (geometric mean / arithmetic
 * mean of the power-spectrum bins; geometric mean in log domain with the
 * LOG_FLOOR) exceeds this are not speech-like — laughter, breath, shouty
 * roughness — and may not enter the embedding. A register shift is exactly
 * what falsely split one voice into "2 speakers": its noise-excited frames
 * form a TIGHT second cloud 0.49–0.80 cosine away that the width-ratio guard
 * cannot catch, so the filter has to happen at the frame level.
 *
 * Placed empirically through this exact window/FFT (same numbers from the
 * numpy engine):
 *   speech-like: vocal-tract-shaped harmonic series, F0 80–350 Hz with
 *     formant/tilt shaping, incl. both fixture voices → flatness 0.001–0.14
 *   laughter/breath-like: white-noise bursts, band noise, aspirated onsets,
 *     noise-dominant harmonic+noise mixes           → flatness 0.18–0.75
 * 0.16 sits in the measured gap. Normal speech loses at most its fricative
 * frames; a laugh loses every frame and the segment embeds empty (so the
 * shared core lets it inherit a neighbour instead of minting a speaker).
 */
const SPEECH_FLATNESS_MAX = 0.16;
/** Fewer voiced frames than this → the segment is unusable (empty embedding). */
const MIN_VOICED_FRAMES = 5;
/** Only the first 4 s of a segment feed its embedding. */
const SEGMENT_EMBED_CAP_S = 4.0;
/** Wall-clock budget for each synchronous stretch of frame extraction. */
const TARGET_CHUNK_MS = 12;
/** Frames per chunk before the first timing measurement calibrates it. */
const INITIAL_FRAMES_PER_CHUNK = 200;
/** Bounds for the adaptive frames-per-chunk count. */
const MIN_FRAMES_PER_CHUNK = 16;
const MAX_FRAMES_PER_CHUNK = 4096;

const FRAME_DIMS = 2 + CEPSTRA; // RMS + spectral flatness + c1..c12

// --- Precomputed tables (deterministic, built once at module load) ----------

const HAMMING = (() => {
  const w = new Float64Array(FRAME_SIZE);
  for (let n = 0; n < FRAME_SIZE; n++) {
    w[n] = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / (FRAME_SIZE - 1));
  }
  return w;
})();

/** Bit-reversal permutation for the radix-2 FFT. */
const BIT_REV = (() => {
  const rev = new Uint16Array(FFT_SIZE);
  for (let i = 0; i < FFT_SIZE; i++) {
    let r = 0;
    let x = i;
    for (let b = 0; b < FFT_BITS; b++) {
      r = (r << 1) | (x & 1);
      x >>= 1;
    }
    rev[i] = r;
  }
  return rev;
})();

/** Twiddle factors e^{-2πik/N} for k in [0, N/2). */
const TWIDDLE = (() => {
  const cos = new Float64Array(FFT_SIZE / 2);
  const sin = new Float64Array(FFT_SIZE / 2);
  for (let k = 0; k < FFT_SIZE / 2; k++) {
    cos[k] = Math.cos((-2 * Math.PI * k) / FFT_SIZE);
    sin[k] = Math.sin((-2 * Math.PI * k) / FFT_SIZE);
  }
  return { cos, sin };
})();

function hzToMel(hz: number): number {
  return 2595 * Math.log10(1 + hz / 700);
}

function melToHz(mel: number): number {
  return 700 * (10 ** (mel / 2595) - 1);
}

/** 24 triangular mel filters over FFT bins 0..256, spanning 80–7600 Hz. */
const MEL_WEIGHTS = (() => {
  const bins = FFT_SIZE / 2 + 1;
  const loMel = hzToMel(MEL_LO_HZ);
  const hiMel = hzToMel(MEL_HI_HZ);
  // 26 edge points in mel space → fractional FFT-bin positions.
  const edges: number[] = [];
  for (let i = 0; i < MEL_FILTER_COUNT + 2; i++) {
    const mel = loMel + ((hiMel - loMel) * i) / (MEL_FILTER_COUNT + 1);
    edges.push((melToHz(mel) * FFT_SIZE) / SAMPLE_RATE);
  }
  const filters: Float64Array[] = [];
  for (let m = 0; m < MEL_FILTER_COUNT; m++) {
    const [left, center, right] = [edges[m], edges[m + 1], edges[m + 2]];
    const w = new Float64Array(bins);
    for (let k = 0; k < bins; k++) {
      if (k > left && k < center) w[k] = (k - left) / (center - left);
      else if (k >= center && k < right) w[k] = (right - k) / (right - center);
    }
    filters.push(w);
  }
  return filters;
})();

/** DCT-II basis rows for c1..c12 over 24 log-mel energies. */
const DCT = (() => {
  const rows: Float64Array[] = [];
  for (let k = 1; k <= CEPSTRA; k++) {
    const row = new Float64Array(MEL_FILTER_COUNT);
    for (let m = 0; m < MEL_FILTER_COUNT; m++) {
      row[m] = Math.cos((Math.PI * k * (m + 0.5)) / MEL_FILTER_COUNT);
    }
    rows.push(row);
  }
  return rows;
})();

// --- FFT --------------------------------------------------------------------

/** In-place iterative radix-2 FFT (512-point; input here is always real). */
function fftInPlace(re: Float64Array, im: Float64Array): void {
  for (let i = 0; i < FFT_SIZE; i++) {
    const j = BIT_REV[i];
    if (j > i) {
      const tr = re[i];
      re[i] = re[j];
      re[j] = tr;
      const ti = im[i];
      im[i] = im[j];
      im[j] = ti;
    }
  }
  for (let size = 2; size <= FFT_SIZE; size <<= 1) {
    const half = size >> 1;
    const step = FFT_SIZE / size;
    for (let base = 0; base < FFT_SIZE; base += size) {
      for (let k = 0; k < half; k++) {
        const wr = TWIDDLE.cos[k * step];
        const wi = TWIDDLE.sin[k * step];
        const a = base + k;
        const b = a + half;
        const tr = re[b] * wr - im[b] * wi;
        const ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
      }
    }
  }
}

// --- Cooperative yielding ---------------------------------------------------
//
// setTimeout(0) is clamped to ~4 ms once timer nesting passes depth 5, which
// turns the ~300 yields of a 20-minute video into >1 s of pure timer wait. A
// MessageChannel post is a macrotask with no nesting clamp: control returns as
// soon as the event loop drains. One channel is created lazily and reused for
// every yield; ref/unref (Node-only, absent in browsers) stop the idle port
// from pinning the Node event loop open between calls.

type UnrefablePort = MessagePort & { ref?: () => void; unref?: () => void };

let yieldChannel: MessageChannel | null = null;
const yieldWaiters: Array<() => void> = [];

function yieldToEventLoop(): Promise<void> {
  if (yieldChannel === null) {
    const channel = new MessageChannel();
    const port1 = channel.port1 as UnrefablePort;
    port1.onmessage = () => {
      const resolve = yieldWaiters.shift();
      if (yieldWaiters.length === 0) port1.unref?.();
      resolve?.();
    };
    yieldChannel = channel;
  }
  const channel = yieldChannel;
  (channel.port1 as UnrefablePort).ref?.();
  return new Promise<void>((resolve) => {
    yieldWaiters.push(resolve);
    channel.port2.postMessage(null);
  });
}

// --- Frame extraction (runs BEFORE the PCM buffer is transferred) -----------

/**
 * Per-frame features for the whole recording: [RMS, spectral flatness,
 * c1..c12] per 20 ms hop.
 *
 * Deterministic (chunk timing changes only when yields happen, never the
 * math); yields between chunks sized adaptively so each synchronous stretch
 * stays near TARGET_CHUNK_MS, keeping a 20-minute video (~60k frames) from
 * blocking paint or stalling on clamped timers.
 */
export async function computeMfccFrames(pcm: Float32Array): Promise<MfccFrames> {
  const hopSeconds = HOP_SIZE / SAMPLE_RATE;
  const numFrames =
    pcm.length >= FRAME_SIZE ? Math.floor((pcm.length - FRAME_SIZE) / HOP_SIZE) + 1 : 0;
  const frames = new Float32Array(numFrames * FRAME_DIMS);

  const re = new Float64Array(FFT_SIZE);
  const im = new Float64Array(FFT_SIZE);
  const mags = new Float64Array(FFT_SIZE / 2 + 1);
  const logMel = new Float64Array(MEL_FILTER_COUNT);

  let framesPerChunk = INITIAL_FRAMES_PER_CHUNK;
  let nextYieldAt = framesPerChunk;
  let chunkStart = performance.now();

  for (let f = 0; f < numFrames; f++) {
    if (f === nextYieldAt) {
      // Re-aim the chunk size at the wall-clock budget. The correction factor
      // is bounded so one noisy measurement cannot swing the size wildly.
      const elapsed = performance.now() - chunkStart;
      const scale = Math.min(4, Math.max(0.25, TARGET_CHUNK_MS / Math.max(elapsed, 0.5)));
      framesPerChunk = Math.min(
        MAX_FRAMES_PER_CHUNK,
        Math.max(MIN_FRAMES_PER_CHUNK, Math.round(framesPerChunk * scale))
      );
      nextYieldAt = f + framesPerChunk;
      await yieldToEventLoop();
      chunkStart = performance.now();
    }
    const off = f * HOP_SIZE;

    let sumSq = 0;
    for (let n = 0; n < FRAME_SIZE; n++) {
      const s = pcm[off + n];
      sumSq += s * s;
      re[n] = s * HAMMING[n];
      im[n] = 0;
    }
    re.fill(0, FRAME_SIZE);
    im.fill(0, FRAME_SIZE);
    frames[f * FRAME_DIMS] = Math.sqrt(sumSq / FRAME_SIZE);

    fftInPlace(re, im);
    for (let k = 0; k <= FFT_SIZE / 2; k++) {
      mags[k] = Math.sqrt(re[k] * re[k] + im[k] * im[k]);
    }

    // Speech-likeness: spectral flatness of the ~300–3000 Hz power spectrum.
    let logSum = 0;
    let powSum = 0;
    for (let k = FLATNESS_LO_BIN; k <= FLATNESS_HI_BIN; k++) {
      const p = mags[k] * mags[k];
      logSum += Math.log(Math.max(p, LOG_FLOOR));
      powSum += p;
    }
    const bandBins = FLATNESS_HI_BIN - FLATNESS_LO_BIN + 1;
    frames[f * FRAME_DIMS + 1] =
      Math.exp(logSum / bandBins) / Math.max(powSum / bandBins, LOG_FLOOR);

    for (let m = 0; m < MEL_FILTER_COUNT; m++) {
      const w = MEL_WEIGHTS[m];
      let energy = 0;
      for (let k = 0; k <= FFT_SIZE / 2; k++) {
        if (w[k] !== 0) energy += w[k] * mags[k];
      }
      logMel[m] = Math.log(Math.max(energy, LOG_FLOOR));
    }

    for (let c = 0; c < CEPSTRA; c++) {
      const row = DCT[c];
      let acc = 0;
      for (let m = 0; m < MEL_FILTER_COUNT; m++) acc += row[m] * logMel[m];
      frames[f * FRAME_DIMS + 2 + c] = acc;
    }
  }

  return { frames, dims: FRAME_DIMS, hopSeconds };
}

// --- Segment embeddings (runs AFTER the transcript arrives) -----------------

/**
 * One embedding per speech segment from the stashed frames: mean+std of
 * c1..c12 over the speech-like voiced frames (RMS ≥ VOICED_RMS AND spectral
 * flatness ≤ SPEECH_FLATNESS_MAX) in the segment's first 4 s, L2-normalized
 * (24 dims). Unusable segments (too quiet, too short, out of range, or all
 * laughter/breath) yield [] — the shared core makes them inherit a
 * neighbour's speaker.
 */
export function segmentEmbeddingsFromFrames(
  frames: Float32Array,
  dims: number,
  hopSeconds: number,
  segments: readonly SpeechSegment[]
): number[][] {
  const cepstra = dims - 2;
  const numFrames = dims > 0 ? Math.floor(frames.length / dims) : 0;

  return segments.map((seg) => {
    if (cepstra < 1 || numFrames === 0 || !(hopSeconds > 0)) return [];
    if (!Number.isFinite(seg.start) || !Number.isFinite(seg.end)) return [];
    const capEnd = Math.min(seg.end, seg.start + SEGMENT_EMBED_CAP_S);
    const first = Math.max(0, Math.ceil(seg.start / hopSeconds - 1e-6));
    const last = Math.min(numFrames - 1, Math.floor(capEnd / hopSeconds + 1e-6));

    const sum = new Float64Array(cepstra);
    const sumSq = new Float64Array(cepstra);
    let voiced = 0;
    for (let f = first; f <= last; f++) {
      if (frames[f * dims] < VOICED_RMS) continue;
      if (frames[f * dims + 1] > SPEECH_FLATNESS_MAX) continue; // not speech-like
      voiced++;
      for (let c = 0; c < cepstra; c++) {
        const v = frames[f * dims + 2 + c];
        sum[c] += v;
        sumSq[c] += v * v;
      }
    }
    if (voiced < MIN_VOICED_FRAMES) return [];

    const emb = new Array<number>(cepstra * 2);
    for (let c = 0; c < cepstra; c++) {
      const mean = sum[c] / voiced;
      const variance = Math.max(0, sumSq[c] / voiced - mean * mean);
      emb[c] = mean;
      emb[cepstra + c] = Math.sqrt(variance);
    }
    let norm = 0;
    for (const v of emb) norm += v * v;
    norm = Math.sqrt(norm);
    if (!Number.isFinite(norm) || norm === 0) return [];
    return emb.map((v) => v / norm);
  });
}
