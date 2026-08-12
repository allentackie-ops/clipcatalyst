/**
 * The delay / minimum-visible state machine behind every branded wait.
 *
 * A loader shown for 80 ms is a flicker, and a loader shown when nothing is
 * loading is theatre that costs real time. So a wait is only ever painted if
 * it survives the delay, and once painted it stays for the minimum:
 *
 *   start ──180ms──▶ visible ──load ends──▶ held to 420ms ──▶ gone
 *     └── load ends first ─────────────────────────────────────▶ never shown
 *
 * The machine is a pure fold over timestamped events. It never reads a clock,
 * never touches the DOM, and imports nothing — `npm run test:loader` compiles
 * this one file on its own and drives it straight from node.
 *
 * Callers do not set timers themselves: `nextDeadline` returns the single
 * moment the machine next needs waking, so there is exactly one pending timer
 * at any time and repeated starts cannot stack them.
 */

/** Wait this long before painting anything. Most navigations finish first. */
export const LOADER_DELAY_MS = 180;
/** Once painted, stay at least this long. Prevents a flash-and-vanish. */
export const LOADER_MIN_VISIBLE_MS = 420;

export type LoaderTiming = {
  /** ms to wait before the loader is painted at all. */
  delayMs: number;
  /** ms the loader stays once painted, even if the load already finished. */
  minVisibleMs: number;
};

export const DEFAULT_TIMING: LoaderTiming = {
  delayMs: LOADER_DELAY_MS,
  minVisibleMs: LOADER_MIN_VISIBLE_MS,
};

/**
 * - `idle`     nothing is loading and nothing is painted
 * - `waiting`  a load is running but has not earned a loader yet
 * - `showing`  the loader is painted and the load is still running
 * - `holding`  the load finished but the minimum-visible window has not
 */
export type LoaderPhase = "idle" | "waiting" | "showing" | "holding";

export type LoaderState = {
  phase: LoaderPhase;
  /** The only thing a view needs. True exactly in `showing` and `holding`. */
  visible: boolean;
  /** When the current load began. Non-null only while `waiting`. */
  waitingSince: number | null;
  /** When the loader was actually painted. Non-null while visible. */
  shownAt: number | null;
};

export type LoaderEvent =
  | { type: "start"; at: number }
  | { type: "finish"; at: number }
  | { type: "tick"; at: number };

/** The resting state. Frozen: it is shared by every caller as an initial value. */
export const IDLE: LoaderState = Object.freeze({
  phase: "idle",
  visible: false,
  waitingSince: null,
  shownAt: null,
});

const showing = (shownAt: number): LoaderState => ({
  phase: "showing",
  visible: true,
  waitingSince: null,
  shownAt,
});

/**
 * Clamp a caller's timing to something the machine can reason about: finite
 * and non-negative. A `delayMs` of 0 means "paint immediately"; a
 * `minVisibleMs` of 0 means "leave as soon as the load ends".
 */
export function normalizeTiming(timing?: Partial<LoaderTiming>): LoaderTiming {
  const clamp = (value: unknown, fallback: number): number =>
    typeof value === "number" && Number.isFinite(value) && value > 0
      ? value
      : typeof value === "number" && Number.isFinite(value)
        ? 0
        : fallback;
  return {
    delayMs: clamp(timing?.delayMs, DEFAULT_TIMING.delayMs),
    minVisibleMs: clamp(timing?.minVisibleMs, DEFAULT_TIMING.minVisibleMs),
  };
}

/**
 * Fold one event into the state. Pure: the input state is never mutated, and
 * the same state object is returned when nothing changed, so a view can bail
 * out on identity.
 */
export function reduceLoader(
  state: LoaderState,
  event: LoaderEvent,
  timing?: Partial<LoaderTiming>,
): LoaderState {
  const { delayMs, minVisibleMs } = normalizeTiming(timing);
  const at = event.at;

  switch (event.type) {
    case "start": {
      switch (state.phase) {
        case "idle":
          // A zero delay means the caller wants the loader now, so skip the
          // waiting phase rather than making them schedule a 0 ms timer.
          return delayMs === 0
            ? showing(at)
            : { phase: "waiting", visible: false, waitingSince: at, shownAt: null };
        case "waiting":
        case "showing":
          // Already counting, or already up. Starting again must not re-arm
          // the delay or extend the window — that is how timers stack.
          return state;
        case "holding":
          // The loader is still on screen from the last load; a new load
          // simply reclaims it. The minimum-visible window it already served
          // stands, so it will not out-stay its welcome later.
          return showing(state.shownAt === null ? at : state.shownAt);
      }
      return state;
    }

    case "finish": {
      switch (state.phase) {
        case "waiting":
          // The whole point: a load that beat the delay shows nothing at all.
          return IDLE;
        case "showing": {
          const until = (state.shownAt === null ? at : state.shownAt) + minVisibleMs;
          if (at >= until) return IDLE;
          return { ...state, phase: "holding" };
        }
        case "idle":
        case "holding":
          return state;
      }
      return state;
    }

    case "tick": {
      switch (state.phase) {
        case "waiting": {
          if (state.waitingSince === null) return state;
          if (at < state.waitingSince + delayMs) return state;
          // Painted now, not at the deadline: the minimum-visible window is
          // measured from the moment the user could actually see it.
          return showing(at);
        }
        case "holding": {
          if (state.shownAt === null) return IDLE;
          if (at < state.shownAt + minVisibleMs) return state;
          return IDLE;
        }
        case "idle":
        case "showing":
          return state;
      }
      return state;
    }
  }

  return state;
}

/**
 * The single moment the machine next needs waking, or null if it is waiting
 * on the outside world (`showing`) or has nothing to do (`idle`).
 *
 * One deadline, never a queue — a caller that keeps exactly one timer alive
 * for this value can never accumulate a second.
 */
export function nextDeadline(
  state: LoaderState,
  timing?: Partial<LoaderTiming>,
): number | null {
  const { delayMs, minVisibleMs } = normalizeTiming(timing);
  if (state.phase === "waiting" && state.waitingSince !== null) {
    return state.waitingSince + delayMs;
  }
  if (state.phase === "holding" && state.shownAt !== null) {
    return state.shownAt + minVisibleMs;
  }
  return null;
}

/**
 * Fold a whole sequence of events — handy for reasoning about a timeline in
 * one expression, and what the tests drive.
 */
export function runLoader(
  events: readonly LoaderEvent[],
  timing?: Partial<LoaderTiming>,
  initial: LoaderState = IDLE,
): LoaderState {
  let state = initial;
  for (const event of events) state = reduceLoader(state, event, timing);
  return state;
}
