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

const TOKEN_KEY = "cc_session";

export type Plan = "free" | "starter" | "pro" | "enterprise";

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
  };
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
  opts: { method?: "GET" | "POST"; body?: unknown; auth?: boolean } = {}
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

const ACTIVE_STATUSES = new Set(["active", "trialing", "past_due"]);

/** Effective plan, mirroring billing.py's rule: the paid plan counts only
 *  while the subscription is alive; canceled/unpaid/none → free. */
export function effectivePlan(user: AccountUser): Plan {
  return ACTIVE_STATUSES.has(user.plan_status) ? user.plan : "free";
}
