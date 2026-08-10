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
// to yield to the macrotask queue every ~200 frames so long videos never
// freeze the UI thread.

import type { SpeechSegment } from "./diarize";

export type MfccFrames = {
  /** numFrames × dims, row-major. Dim 0 = frame RMS, dims 1..12 = c1..c12. */
  frames: Float32Array;
  /** Values per frame (1 RMS + 12 cepstral coefficients). */
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
/** Fewer voiced frames than this → the segment is unusable (empty embedding). */
const MIN_VOICED_FRAMES = 5;
/** Only the first 4 s of a segment feed its embedding. */
const SEGMENT_EMBED_CAP_S = 4.0;
/** Yield to the macrotask queue this often during frame extraction. */
const YIELD_EVERY_FRAMES = 200;

const FRAME_DIMS = 1 + CEPSTRA;

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

// --- Frame extraction (runs BEFORE the PCM buffer is transferred) -----------

/**
 * Per-frame features for the whole recording: [RMS, c1..c12] per 20 ms hop.
 *
 * Deterministic; yields to the macrotask queue every ~200 frames so a
 * 20-minute video (~60k frames) never blocks paint.
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

  for (let f = 0; f < numFrames; f++) {
    if (f > 0 && f % YIELD_EVERY_FRAMES === 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
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
      frames[f * FRAME_DIMS + 1 + c] = acc;
    }
  }

  return { frames, dims: FRAME_DIMS, hopSeconds };
}

// --- Segment embeddings (runs AFTER the transcript arrives) -----------------

/**
 * One embedding per speech segment from the stashed frames: mean+std of
 * c1..c12 over the voiced frames in the segment's first 4 s, L2-normalized
 * (24 dims). Unusable segments (too quiet, too short, out of range) yield []
 * — the shared core makes them inherit a neighbour's speaker.
 */
export function segmentEmbeddingsFromFrames(
  frames: Float32Array,
  dims: number,
  hopSeconds: number,
  segments: readonly SpeechSegment[]
): number[][] {
  const cepstra = dims - 1;
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
      voiced++;
      for (let c = 0; c < cepstra; c++) {
        const v = frames[f * dims + 1 + c];
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
