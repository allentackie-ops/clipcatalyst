// Typed client for the ClipCatalyst accounts + billing API (ACCOUNTS.md):
// register / login / logout / me / checkout / portal. The API base is
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
  entitlements: {
    max_height: number;
    watermark_required: boolean;
    clips_per_month: number | null;
    /** Whether renders may carry this account's own logo and caption colour.
     *  Its own promise — never inferred from `watermark_required`, which is a
     *  different one that merely lines up today (BRANDKIT.md). Optional so a
     *  server from before brand kits still parses. */
    brand_kit?: boolean;
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
      "Couldn't reach the ClipCatalyst server — check your connection and try again.",
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

const ACTIVE_STATUSES = new Set(["active", "trialing", "past_due"]);

/** Effective plan, mirroring billing.py's rule: the paid plan counts only
 *  while the subscription is alive; canceled/unpaid/none → free. */
export function effectivePlan(user: AccountUser): Plan {
  return ACTIVE_STATUSES.has(user.plan_status) ? user.plan : "free";
}
