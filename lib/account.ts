// Typed client for the ClipCatalyst accounts + billing API (ACCOUNTS.md):
// register / login / logout / me / checkout / portal, the clip library
// (LIBRARY.md) and connected-channel publishing (PUBLISH.md). The API base is
// cloud.ts's CLOUD_API — exactly one env (NEXT_PUBLIC_CLOUD_API) decides
// whether any of this is live.
//
// The session token lives in localStorage["cc_session"]. That is a deliberate
// XSS tradeoff, noted here per the spec: the frontend is a static export on
// github.io, cross-origin from the API, so httpOnly cookies aren't an option.
// Any script that can run on this page could read the token — the site ships
// no third-party scripts, which is the surface that matters.

import { CLOUD_API, setCloudTokenSource } from "@/components/studio/cloud";
import { MAX_LOGO_BYTES, type BrandKit } from "@/lib/studio/brandkit";

const TOKEN_KEY = "cc_session";

export type Plan = "free" | "starter" | "pro" | "enterprise";

/** A stored brand kit as the server reports it — the logo as a URL, never
 *  bytes (BrandKitOut in api/clipcatalyst_api/models.py). */
export type ServerBrandKit = {
  logo_url: string | null;
  caption_color: string | null;
  show_logo: boolean;
};

/** GET /v1/me — the single account source the frontend trusts. */
export type AccountUser = {
  email: string;
  plan: Plan;
  plan_status: string;
  /** limit null = unlimited. */
  quota: { limit: number | null; used: number; month: string };
  /** How this account can be signed into: any of "password", "google" and
   *  "email" (LIBRARY.md Part 1, EMAILAUTH.md). Optional so a server from
   *  before Google sign-in still parses; absent means "the server didn't
   *  say", which the UI treats as nothing to show rather than as "no way in".
   *  "email" is present whenever the server has a mailer — a code goes to an
   *  address, and every account has one. */
  auth_methods?: string[];
  entitlements: {
    max_height: number;
    watermark_required: boolean;
    clips_per_month: number | null;
    /** Whether renders may carry this account's own logo and caption colour.
     *  Its own promise — never inferred from `watermark_required`, which is a
     *  different one that merely lines up today (BRANDKIT.md). Optional so a
     *  server from before brand kits still parses. */
    brand_kit?: boolean;
    /** How long a saved clip's FILE is kept, in days; null = forever. The
     *  three states are all distinct and the retention line reads all three:
     *  a number is a deadline, null is "kept as long as the account is", and
     *  `undefined` is a server from before the library that has not made a
     *  promise at all — so the UI stays quiet rather than inventing one. */
    retention_days?: number | null;
  };
  /** The kit stored server-side. Present whatever the plan says — a kit
   *  outlives a downgrade — so `entitlements.brand_kit` is what decides
   *  whether a cloud render uses it. Optional for the same reason above. */
  brand?: ServerBrandKit;
};

/** API failure with the HTTP status so callers can branch on 401/402/503.
 *  status 0 = the request never reached the server. */
export class AccountError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "AccountError";
    this.status = status;
  }
}

/** Read the stored session token (null = signed out; always null during
 *  prerender — there is no window). */
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage blocked (private mode etc.) — treat as signed out
  }
}

function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (token === null) window.localStorage.removeItem(TOKEN_KEY);
    else window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage blocked — the session just won't survive a reload.
  }
}

// Cloud engine calls (createJob/upload/start/poll/files) carry the same
// session, so the server can enforce quota, height, and watermark per plan.
setCloudTokenSource(getToken);

async function request<T>(
  path: string,
  opts: {
    method?: "GET" | "POST" | "PUT" | "DELETE";
    body?: unknown;
    auth?: boolean;
  } = {}
): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  let res: Response;
  try {
    res = await fetch(`${CLOUD_API}${path}`, {
      method: opts.method ?? "POST",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch {
    throw new AccountError(
      "Couldn't reach the ClipCatalyst server. Check your connection and try again.",
      0
    );
  }
  if (!res.ok) {
    // FastAPI errors arrive as {detail: "..."} — surface them when present.
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error body; fall through to the status-code message.
    }
    // 401 on an authenticated call means the session expired or was revoked
    // — drop the stale token so every consumer agrees we're signed out.
    // (A 401 from login itself must NOT clear a different, valid session.)
    if (res.status === 401 && opts.auth) setToken(null);
    throw new AccountError(
      detail || `Request failed (${res.status}).`,
      res.status
    );
  }
  return (await res.json()) as T;
}

type AuthResponse = { token: string; user: unknown };

/** POST /v1/auth/register — creates the account and signs in (stores the
 *  token). The caller should refresh /v1/me afterwards. */
export async function register(email: string, password: string): Promise<void> {
  const res = await request<AuthResponse>("/v1/auth/register", {
    body: { email, password },
  });
  setToken(res.token);
}

/** POST /v1/auth/login — signs in (stores the token). */
export async function login(email: string, password: string): Promise<void> {
  const res = await request<AuthResponse>("/v1/auth/login", {
    body: { email, password },
  });
  setToken(res.token);
}

/**
 * POST /v1/auth/google — trade a Google ID token for a session (stores it).
 *
 * Identity only, never publishing (LIBRARY.md Part 1): Google says who this
 * is, and what comes back is the same session `/v1/auth/login` mints. The
 * token itself is believed by nobody here — the server verifies its signature
 * against Google's keys, its `aud`, and `email_verified` before it counts.
 * 503 means the server has no `CC_GOOGLE_CLIENT_ID`; the button that produced
 * this token shouldn't have rendered, so the caller surfaces it verbatim.
 */
export async function signInWithGoogle(idToken: string): Promise<void> {
  const res = await request<AuthResponse>("/v1/auth/google", {
    body: { id_token: idToken },
  });
  setToken(res.token);
}

/**
 * POST /v1/auth/email/start — mail a 6-digit sign-in code to an address.
 *
 * Always answers the same 200 whether or not the address has an account
 * (EMAILAUTH.md): the account is created or found when the code is VERIFIED,
 * so there is nothing here for anyone to enumerate with — and nothing for
 * this function to report back beyond "it went". A code, not a magic link,
 * because corporate mail scanners and link-preview bots fetch URLs in
 * incoming mail and would consume a one-time link before its owner ever
 * clicked it.
 *
 * 503 means the server has no mailer (CC_MAILER=none) or could not send;
 * 429 means too many codes for that address or from this machine. Both
 * arrive as the server's own sentence, which the caller shows verbatim.
 */
export async function startEmailCode(email: string): Promise<void> {
  await request<{ sent: boolean }>("/v1/auth/email/start", { body: { email } });
}

/**
 * POST /v1/auth/email/verify — trade the code for a session (stores it).
 *
 * The session that comes back is the one `/v1/auth/login` mints: same TTL,
 * same bearer, same everything downstream. A 401 covers wrong, expired,
 * already-used and never-requested alike — deliberately one answer, so the
 * endpoint cannot be asked which addresses have codes in flight — so the
 * caller says "that code didn't work, request a new one" and never tries to
 * infer more than that.
 */
export async function verifyEmailCode(
  email: string,
  code: string
): Promise<void> {
  const res = await request<AuthResponse>("/v1/auth/email/verify", {
    body: { email, code },
  });
  setToken(res.token);
}

/** POST /v1/auth/logout — revokes the session server-side, forgets it
 *  locally either way (offline or already-expired still signs out here). */
export async function logout(): Promise<void> {
  try {
    await request<unknown>("/v1/auth/logout", { auth: true });
  } catch {
    // Best-effort revoke; an unreachable server can't keep us signed in.
  } finally {
    setToken(null);
  }
}

/** GET /v1/me. Throws AccountError(401) when the session is gone (the stale
 *  token is already cleared by then). */
export function me(): Promise<AccountUser> {
  return request<AccountUser>("/v1/me", { method: "GET", auth: true });
}

/** POST /v1/billing/checkout {plan} → Stripe Checkout url. Billing off →
 *  the server's honest 503 message. */
export function checkout(plan: Exclude<Plan, "free">): Promise<{ url: string }> {
  return request<{ url: string }>("/v1/billing/checkout", {
    auth: true,
    body: { plan },
  });
}

/** POST /v1/billing/portal → Stripe billing portal url. */
export function portal(): Promise<{ url: string }> {
  return request<{ url: string }>("/v1/billing/portal", { auth: true });
}

// ---- Brand kit (BRANDKIT.md) ----
// The kit itself is local-first — it lives in the browser so Studio works with
// no account at all (lib/studio/brandkit.ts). These two calls are the SYNC:
// they keep the server's copy identical so a cloud render draws the same
// corner the browser does. Neither is on the path of a render.

/**
 * PUT /v1/me/brand — replaces the WHOLE kit. An absent logo means the kit has
 * none, not "keep whatever is stored": the panel holds the complete kit
 * locally and syncs it whole, and a PUT that meant anything else would stop
 * being a PUT.
 *
 * The logo travels as the same `data:` URL the browser already stores; the
 * server decodes it, measures the real bytes, and sniffs the content type
 * rather than trusting anything sent with it. Throws AccountError(403) when
 * the plan carries no brand kit — the caller surfaces that verbatim.
 */
export function putBrand(kit: BrandKit): Promise<ServerBrandKit> {
  return request<ServerBrandKit>("/v1/me/brand", {
    method: "PUT",
    auth: true,
    body: {
      logo: kit.logo?.dataUrl ?? null,
      caption_color: kit.captionColor,
      show_logo: kit.showLogo,
    },
  });
}

/** DELETE /v1/me/brand — clears the stored kit, logo file included.
 *  Deliberately NOT plan-gated server-side (main.py says why): taking your
 *  own logo off our servers must not depend on what you're paying today. */
export function deleteBrand(): Promise<ServerBrandKit> {
  return request<ServerBrandKit>("/v1/me/brand", {
    method: "DELETE",
    auth: true,
  });
}

/**
 * GET /v1/me/brand/logo → the stored logo as a `data:` URL, so a creator
 * signing in on a new device gets their kit back instead of an empty panel.
 *
 * Never throws and never rejects: null covers "no logo", a failed request, a
 * file past the panel's own ceiling, and a browser with no FileReader. This
 * is a convenience on top of the local kit — it must not be able to break the
 * panel, let alone a render.
 */
export async function fetchBrandLogo(): Promise<string | null> {
  const token = getToken();
  if (token === null || typeof FileReader === "undefined") return null;
  try {
    const res = await fetch(`${CLOUD_API}/v1/me/brand/logo`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    const blob = await res.blob();
    if (blob.size === 0 || blob.size > MAX_LOGO_BYTES) return null;
    return await new Promise<string | null>((resolve) => {
      const reader = new FileReader();
      reader.onload = () =>
        resolve(typeof reader.result === "string" ? reader.result : null);
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(blob);
    });
  } catch {
    return null;
  }
}

// ---- The clip library (LIBRARY.md Part 2) ----
// Two lifetimes in one row, and every type below keeps them apart: the
// metadata is permanent, the FILE expires. `available` and `url` describe the
// file and nothing else — a card whose retention window has closed still has
// its title, score and hooks, which is why the library UI can show one
// without ever reaching for a player.

/** One transcript word on a saved clip (ClipWordOut in models.py). Times are
 *  relative to the clip's own start, not the source video's. */
export type ClipWord = {
  text: string;
  start: number;
  end: number;
  /** Diarized speaker index; null = unknown (JSON's spelling of absent). */
  speaker?: number | null;
};

/** A saved clip as `GET /v1/clips` reports it (ClipSummaryOut). */
export type LibraryClip = {
  id: string;
  /** '' for a browser clip — nothing was rendered on our hardware. */
  job_id: string;
  clip_index: number;
  title: string;
  score: number;
  hooks: string[];
  reason: string;
  tip: string;
  /** Seconds into the source video the clip was cut from. */
  start: number;
  end: number;
  duration: number;
  width: number;
  height: number;
  speaker_count: number;
  /** "cloud" (rendered on the server) | "browser" (uploaded on purpose). */
  engine: string;
  bytes: number;
  created_at: string;
  /** When the FILE goes; null = never (an unlimited plan). The row has no
   *  expiry of its own — deleting it is the owner's decision alone. */
  expires_at: string | null;
  /** Is the video still here? Everything above is here regardless. */
  available: boolean;
  /** Only when available. May be relative (resolved against CLOUD_API), our
   *  API's own absolute url, or a presigned link at the storage bucket. */
  url: string | null;
};

/** One clip in full — the transcript is the half that outlives the file
 *  (`GET /v1/clips/{id}`, and what an upload hands back). */
export type LibraryClipDetail = LibraryClip & { words: ClipWord[] };

/** One page of the library, newest first. `next_before` is opaque: hand it
 *  straight back as `before` for the next page; null = the end. */
export type ClipPage = { clips: LibraryClip[]; next_before: string | null };

/** Page size for the library grid. The server's own ceiling is 50; this is
 *  a few rows of cards, which is what a "load more" is for. */
export const CLIPS_PAGE_SIZE = 24;

/**
 * GET /v1/clips — this account's clips, newest first.
 *
 * `before` is the previous page's `next_before` and nothing else: the cursor
 * is the server's business (it carries a timestamp AND an id, because one
 * render writes several clips in the same millisecond), so this never builds
 * or parses one.
 */
export function listClips(
  before?: string | null,
  limit: number = CLIPS_PAGE_SIZE
): Promise<ClipPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before) params.set("before", before);
  return request<ClipPage>(`/v1/clips?${params.toString()}`, {
    method: "GET",
    auth: true,
  });
}

/** DELETE /v1/clips/{id} — the row AND the file, the one thing that removes a
 *  library row for good. Retention only ever takes the file. */
export async function deleteClip(id: string): Promise<void> {
  await request<{ ok: boolean }>(`/v1/clips/${encodeURIComponent(id)}`, {
    method: "DELETE",
    auth: true,
  });
}

/** The metadata part of an upload — the card the browser already holds.
 *  Nothing here is trusted for anything but display: the server forces
 *  `engine`, measures the bytes itself, and takes the expiry from the plan. */
export type ClipUploadMetadata = {
  title: string;
  score: number;
  hooks: string[];
  reason: string;
  tip: string;
  start: number;
  end: number;
  duration: number;
  width: number;
  height: number;
  speaker_count: number;
  clip_index: number;
  words: ClipWord[];
};

/**
 * POST /v1/clips/upload — save a clip the BROWSER rendered.
 *
 * Only ever called from a click (LIBRARY.md's non-negotiable: never on a
 * timer, never on completion, never behind a pre-ticked box), because the
 * site's promise is that the video stays on the device unless its owner says
 * otherwise.
 *
 * XMLHttpRequest rather than fetch for the same reason cloud.ts's
 * `uploadSource` uses it: only XHR reports real upload progress, and a
 * hundred-megabyte upload with no progress is indistinguishable from a hang.
 * The browser's own multipart encoder sets the Content-Length the route
 * requires. A failure rejects and changes nothing — the local file is
 * untouched, so the caller can offer the click again.
 */
export function uploadClip(
  file: Blob,
  filename: string,
  metadata: ClipUploadMetadata,
  onProgress?: (fraction: number) => void
): Promise<LibraryClipDetail> {
  return new Promise((resolve, reject) => {
    const token = getToken();
    if (token === null) {
      reject(
        new AccountError(
          "You're signed out. Sign in to save clips to your library.",
          401
        )
      );
      return;
    }
    const form = new FormData();
    // The name rides on the part, so no File constructor is needed (and a
    // Blob straight from the renderer works unchanged).
    form.append("file", file, filename);
    form.append("metadata", JSON.stringify(metadata));

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${CLOUD_API}/v1/clips/upload`);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      // FastAPI errors arrive as {detail: "..."} — surface them when present.
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText) as unknown;
      } catch {
        // Non-JSON body; fall through to the status-code message.
      }
      if (xhr.status >= 200 && xhr.status < 300 && body !== null) {
        onProgress?.(1);
        resolve(body as LibraryClipDetail);
        return;
      }
      const detail = (body as { detail?: unknown } | null)?.detail;
      // Same rule as request(): a 401 on an authenticated call means the
      // session is gone, so every consumer should agree we're signed out.
      if (xhr.status === 401) setToken(null);
      reject(
        new AccountError(
          typeof detail === "string" && detail
            ? detail
            : `Saving that clip failed (${xhr.status}).`,
          xhr.status
        )
      );
    };
    xhr.onerror = () =>
      reject(
        new AccountError(
          "Couldn't reach the ClipCatalyst server, so the clip is still on your device.",
          0
        )
      );
    xhr.onabort = () =>
      reject(new AccountError("Saving that clip was canceled.", 0));
    xhr.send(form);
  });
}

/**
 * True for a url that points at OUR API — the only place the session bearer
 * belongs. Relative urls always do; an absolute one only when it starts with
 * the configured API origin, so a presigned link at the storage bucket never
 * sees the token (it carries its own auth in the query string, and SigV4
 * rejects a request that arrives with a second Authorization header anyway).
 */
function isApiUrl(url: string): boolean {
  if (!/^https?:\/\//.test(url)) return true;
  return CLOUD_API !== "" && url.startsWith(`${CLOUD_API}/`);
}

/** A playable source for a saved clip. `objectUrl` says who owns it: true
 *  means the caller minted it and must revoke it when the player goes.
 *  `mimeType` is the container the server actually served ("" when the bytes
 *  never passed through us), so a download can be named honestly instead of
 *  guessed at. */
export type ClipSource = {
  url: string;
  objectUrl: boolean;
  mimeType: string;
};

/**
 * Resolve a saved clip into something a `<video>` can play.
 *
 * `/v1/clips/{id}/file` is owner-only and `<video src>` cannot send headers,
 * so our own files are fetched with the session and served from an object URL
 * — the same trick cloud.ts plays for a signed-in job's clips. A presigned
 * bucket url needs none of that and is handed back as-is.
 *
 * Never called for an expired clip: the card shows the Expired state instead
 * of a player, which is the whole point of `available`.
 */
export async function loadClipSource(clip: LibraryClip): Promise<ClipSource> {
  const raw = clip.url ?? "";
  if (!clip.available || raw === "") {
    throw new AccountError(
      "That clip's video has expired, but its card is still here.",
      404
    );
  }
  const url = /^https?:\/\//.test(raw) ? raw : `${CLOUD_API}${raw}`;
  if (!isApiUrl(raw)) return { url, objectUrl: false, mimeType: "" };
  const token = getToken();
  if (token === null) {
    throw new AccountError("You're signed out. Sign in to play this clip.", 401);
  }
  let res: Response;
  try {
    res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  } catch {
    throw new AccountError(
      "Couldn't reach the ClipCatalyst server. Check your connection and try again.",
      0
    );
  }
  if (!res.ok) {
    if (res.status === 401) setToken(null);
    throw new AccountError(
      res.status === 404
        ? "That clip's video is no longer on the server."
        : `Couldn't load that clip (${res.status}).`,
      res.status
    );
  }
  const blob = await res.blob();
  return {
    url: URL.createObjectURL(blob),
    objectUrl: true,
    mimeType: blob.type,
  };
}

// ---- Connected channels and posting (PUBLISH.md Parts 2–4) ----
// Two ideas run through everything below, and both come from the server:
//
//   * whether a platform can be CONNECTED, and
//   * whether a post to it can currently COMPLETE.
//
// They are different questions with different answers (TikTok is known and
// neither; YouTube is both, on a box that has been given credentials), and
// neither is something this file is allowed to have an opinion about. The
// frontend renders what `platforms` says, so a review landing — or a key
// being configured — changes the UI without changing this code.

/** One connected channel (ConnectionOut in models.py). No tokens: the
 *  response model has nowhere to put one, and neither does this type. */
export type Connection = {
  id: string;
  /** 'youtube' … — matches a `PublishPlatform.platform`. */
  platform: string;
  /** What a creator recognises: their channel's title. */
  account_name: string;
  /** The provider's own id for it, so two channels can be told apart. */
  account_id: string;
  scopes: string[];
  created_at: string;
  updated_at: string;
};

/** A platform this product knows about, and what it can do here today
 *  (PlatformOut in models.py). Sent for every platform, connected or not. */
export type PublishPlatform = {
  platform: string;
  label: string;
  /** Can an authorization be started at all right now? */
  connectable: boolean;
  /** Why not, in words a creator reads. '' when it can be connected. */
  reason: string;
  /** The standing caveat about posting here (uploads land private…). */
  note: string;
  /** Is there an adapter in this build that can finish a post? Connectable
   *  and publishable are separate promises; the Post button follows THIS. */
  publishable: boolean;
  /** The visibilities a post may be given, in the order to show them. One
   *  entry means there is no choice — render the list and it is impossible
   *  to offer an option the server would silently override. */
  privacy_choices: string[];
  /** Set when the platform allows no choice, to the value it forces. */
  forced_privacy: string;
};

/** GET /v1/connections — this account's channels, and the whole catalogue. */
export type ConnectionList = {
  connections: Connection[];
  platforms: PublishPlatform[];
};

/** One post attempt (PublishJobOut in models.py) — the same shape a render
 *  job's status has, because it is the same kind of thing to a browser: a
 *  status, a fraction, a line of prose, and an error written to be read. */
export type PublishJob = {
  id: string;
  clip_id: string;
  platform: string;
  /** queued | uploading | done | failed. */
  status: string;
  /** 0..1. */
  progress: number;
  detail: string;
  error: string | null;
  title: string;
  /** What the platform's capability made of the request — not what was asked
   *  for. `private` while Google's review is outstanding. */
  privacy: string;
  video_id: string | null;
  video_url: string | null;
  /** The standing caveat, carried on the job so the sheet keeps saying it. */
  note: string;
  created_at: string;
  updated_at: string;
};

/** True once this post has stopped moving, either way. */
export function publishSettled(job: PublishJob): boolean {
  return job.status === "done" || job.status === "failed";
}

/** GET /v1/connections. Never returns a token — there is no field for one. */
export function listConnections(): Promise<ConnectionList> {
  return request<ConnectionList>("/v1/connections", {
    method: "GET",
    auth: true,
  });
}

/**
 * POST /v1/connections/{platform}/start → where to send the browser.
 *
 * The state and PKCE challenge are inside the URL; the verifier stays on the
 * server, bound to this session. The caller navigates — it does not open a
 * popup — because the provider's consent screen is a full page and a popup is
 * the thing phone browsers block.
 *
 * 503 is the honest deployment answer (no CC_TOKEN_KEY, no client id, no
 * public base URL) and arrives as the server's own sentence. A button that
 * could produce it shouldn't have rendered — `PublishPlatform.connectable`
 * says so — which is why the caller surfaces it verbatim rather than dressing
 * it up.
 */
export function startConnection(
  platform: string
): Promise<{ authorize_url: string }> {
  return request<{ authorize_url: string }>(
    `/v1/connections/${encodeURIComponent(platform)}/start`,
    { auth: true }
  );
}

/** DELETE /v1/connections/{id} — revokes with the provider, THEN deletes.
 *  A 502 means the provider couldn't be reached and nothing was removed: the
 *  stored tokens are the only thing that can ever revoke that grant, so they
 *  survive a failed attempt and the caller offers the click again. */
export async function disconnectConnection(id: string): Promise<void> {
  await request<{ ok: boolean }>(`/v1/connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
    auth: true,
  });
}

/** What the post sheet sends. Both text fields may be empty, and what an
 *  empty one means is the adapter's rule (the clip's own title; its top hook)
 *  rather than a default invented here — so the same post is produced
 *  whichever client is calling. */
export type PublishRequest = {
  platform: string;
  title: string;
  description: string;
  /** A REQUEST. The platform's capability decides, and the job says which. */
  privacy: string;
};

/**
 * POST /v1/clips/{id}/publish — queue an upload of one library clip.
 *
 * Returns the job to poll. A second call while one is already in flight is
 * not an error: the server hands back the job that is already running (200
 * instead of 202), because a double-tapped button is a double-tapped button
 * and two uploads would spend two of a day's small handful on one video.
 *
 * The refusals are all sentences: 409 for an expired clip or no connected
 * channel, 503 for a platform this build cannot post to, 404 for somebody
 * else's clip. Each is shown as it arrives.
 */
export function publishClip(
  clipId: string,
  body: PublishRequest
): Promise<PublishJob> {
  return request<PublishJob>(
    `/v1/clips/${encodeURIComponent(clipId)}/publish`,
    { auth: true, body }
  );
}

/** GET /v1/publishes/{id} — poll one post. Somebody else's is a 404. */
export function getPublish(publishId: string): Promise<PublishJob> {
  return request<PublishJob>(
    `/v1/publishes/${encodeURIComponent(publishId)}`,
    { method: "GET", auth: true }
  );
}

/** The catalogue entry for one platform, or null when this build's server
 *  has never heard of it (an older API, or a platform since removed). */
export function platformFor(
  list: ConnectionList | null,
  platform: string
): PublishPlatform | null {
  return list?.platforms.find((p) => p.platform === platform) ?? null;
}

/** This account's connection for one platform, or null. First wins: a second
 *  channel on the same platform is possible in the schema, and the card that
 *  posts needs one destination, not a list. */
export function connectionFor(
  list: ConnectionList | null,
  platform: string
): Connection | null {
  return list?.connections.find((c) => c.platform === platform) ?? null;
}

/** True when at least one platform can currently complete a post AND has a
 *  channel connected — i.e. a Post control could really appear somewhere. The
 *  question is asked platform-agnostically on purpose: a caller that wants to
 *  know "is any of this worth setting up?" should not have to name YouTube. */
export function canPublish(list: ConnectionList | null): boolean {
  return (
    list?.platforms.some(
      (platform) =>
        platform.publishable &&
        list.connections.some((c) => c.platform === platform.platform)
    ) ?? false
  );
}

/**
 * The library rows one CLOUD job wrote, keyed by `clip_index`.
 *
 * Studio's cloud results are library clips — the worker wrote their rows as it
 * rendered them — but the job's own clip ids are not the library's, so posting
 * one needs this lookup. It reads a single newest-first page: the rows were
 * written seconds ago, so they are at the top of it.
 *
 * Never throws. An empty map is the honest outcome of a failed request, a
 * signed-out session, or a library write that didn't happen (the worker's is
 * best-effort) — and it costs a Post button that would have 404'd, which is
 * the right trade in every one of those cases.
 */
export async function libraryClipsForJob(
  jobId: string
): Promise<Map<number, LibraryClip>> {
  const found = new Map<number, LibraryClip>();
  if (jobId === "" || getToken() === null) return found;
  try {
    const page = await listClips(null, 50);
    for (const clip of page.clips) {
      if (clip.job_id === jobId && !found.has(clip.clip_index)) {
        found.set(clip.clip_index, clip);
      }
    }
  } catch {
    // A library we couldn't read is a library we don't offer to post from.
  }
  return found;
}

const ACTIVE_STATUSES = new Set(["active", "trialing", "past_due"]);

/** Effective plan, mirroring billing.py's rule: the paid plan counts only
 *  while the subscription is alive; canceled/unpaid/none → free. */
export function effectivePlan(user: AccountUser): Plan {
  return ACTIVE_STATUSES.has(user.plan_status) ? user.plan : "free";
}
