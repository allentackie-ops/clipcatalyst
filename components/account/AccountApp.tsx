"use client";

// /account — every account state in one client component, static-export
// safe (query params come from window.location in an effect, never from
// server APIs):
//
//   1. API unset       → honest "activates when the server is live" card
//   2. signed out      → sign in / create account (one card, tabbed)
//   3. signed in       → plan + usage + upgrades + billing portal + sign out
//   4. checkout return → ?checkout=success polls /v1/me (2 s, ~20 s budget)
//      until the Stripe webhook lands; ?checkout=cancelled is dismissible
//
// A ?plan=starter|pro deep link (the pricing CTAs) auto-starts checkout for
// that plan exactly once, as soon as the visitor is signed in.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { Badge, Button, Card, Container, Eyebrow } from "@/components/ui";
import BrandKitPanel from "@/components/studio/BrandKitPanel";
import { cloudEnabled } from "@/components/studio/cloud";
import {
  checkout,
  effectivePlan,
  login,
  portal,
  register,
  type AccountUser,
  type Plan,
} from "@/lib/account";
import { useAccount } from "./AccountProvider";
import GoogleSignInButton from "./GoogleSignInButton";
import LibrarySection from "./LibrarySection";

const PLAN_LABELS: Record<Plan, string> = {
  free: "Free",
  starter: "Starter",
  pro: "Pro",
  enterprise: "Enterprise",
};

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  trialing: "Trial",
  past_due: "Past due",
  canceled: "Canceled",
  unpaid: "Unpaid",
};

/** Display copy only — entitlement VALUES always come from /v1/me. */
const PAID_PLANS: {
  id: Exclude<Plan, "free">;
  name: string;
  price: string;
  blurb: string;
}[] = [
  { id: "starter", name: "Starter", price: "$19/mo", blurb: "30 clips / month · 1080p · no watermark" },
  { id: "pro", name: "Pro", price: "$49/mo", blurb: "100 clips / month · 4K export" },
  { id: "enterprise", name: "Enterprise", price: "from $99/mo", blurb: "Unlimited clips · 4K export" },
];

const INPUT_CLS =
  "w-full rounded-full border border-line-strong bg-white/5 px-5 py-3 text-sm text-white outline-none transition-colors placeholder:text-zinc-500 focus:border-brand-400/70 focus:ring-2 focus:ring-brand-400/40";

/** 1280 → "720p", 1920 → "1080p", 3840 → "4K" (max_height is the long side). */
function heightLabel(maxHeight: number): string {
  if (maxHeight >= 3840) return "4K";
  if (maxHeight >= 1920) return "1080p";
  return "720p";
}

// ---- 1. API unset ----------------------------------------------------------

function OfflineCard() {
  return (
    <div className="mx-auto max-w-xl text-center">
      <Card className="p-8 sm:p-12">
        <Eyebrow>Account</Eyebrow>
        <h1
          tabIndex={-1}
          className="font-display text-2xl font-semibold tracking-tight text-white outline-none sm:text-3xl"
        >
          Accounts aren&rsquo;t live yet
        </h1>
        <p className="mx-auto mt-4 max-w-md text-sm leading-relaxed text-zinc-400">
          Sign-in, plans, and billing activate when the ClipCatalyst server
          is live. Until then, Studio runs entirely in your browser — free,
          no account needed.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Button href="/studio">Open Studio</Button>
          <Button href="/#pricing" variant="ghost">
            See the plans
          </Button>
        </div>
      </Card>
    </div>
  );
}

// ---- 2. Signed out: one card, tabbed ---------------------------------------

const AUTH_TABS = [
  { id: "signin", label: "Sign in" },
  { id: "register", label: "Create account" },
] as const;

type AuthMode = (typeof AUTH_TABS)[number]["id"];

function AuthCard() {
  const { refresh } = useAccount();
  const [mode, setMode] = useState<AuthMode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const signinTabRef = useRef<HTMLButtonElement>(null);
  const registerTabRef = useRef<HTMLButtonElement>(null);

  const switchMode = (next: AuthMode, focusTab: boolean) => {
    setMode(next);
    setError(null);
    if (focusTab) {
      (next === "signin" ? signinTabRef : registerTabRef).current?.focus();
    }
  };

  // Roving tabindex: arrows (and Home/End) move both selection and focus.
  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const next: AuthMode =
      e.key === "Home"
        ? "signin"
        : e.key === "End"
          ? "register"
          : mode === "signin"
            ? "register"
            : "signin";
    if (next !== mode) switchMode(next, true);
  };

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      if (mode === "signin") await login(email, password);
      else await register(email, password);
      await refresh(); // flips the page to the dashboard
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Something went wrong — try again."
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-6 sm:p-8">
      <div
        role="tablist"
        aria-label="Sign in or create account"
        className="grid grid-cols-2 gap-1 rounded-full border border-line bg-ink-950 p-1"
      >
        {AUTH_TABS.map((tab) => {
          const selected = mode === tab.id;
          return (
            <button
              key={tab.id}
              ref={tab.id === "signin" ? signinTabRef : registerTabRef}
              type="button"
              role="tab"
              id={`auth-tab-${tab.id}`}
              aria-selected={selected}
              aria-controls="auth-panel"
              tabIndex={selected ? 0 : -1}
              onClick={() => switchMode(tab.id, false)}
              onKeyDown={onTabKeyDown}
              className={`rounded-full px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
                selected
                  ? "bg-brand-500/15 text-brand-300"
                  : "text-zinc-400 hover:text-white"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      <form
        id="auth-panel"
        role="tabpanel"
        aria-labelledby={`auth-tab-${mode}`}
        onSubmit={handleSubmit}
        className="mt-6 flex flex-col gap-4"
      >
        <div className="flex flex-col gap-2">
          <label
            htmlFor="account-email"
            className="text-sm font-medium text-zinc-300"
          >
            Email
          </label>
          <input
            id="account-email"
            type="email"
            required
            autoComplete="email"
            placeholder="you@studio.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={INPUT_CLS}
          />
        </div>
        <div className="flex flex-col gap-2">
          <label
            htmlFor="account-password"
            className="text-sm font-medium text-zinc-300"
          >
            Password
          </label>
          <input
            id="account-password"
            type="password"
            required
            minLength={mode === "register" ? 8 : undefined}
            autoComplete={
              mode === "signin" ? "current-password" : "new-password"
            }
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={INPUT_CLS}
          />
          {mode === "register" ? (
            <p className="text-xs text-zinc-500">At least 8 characters.</p>
          ) : null}
        </div>

        <div aria-live="polite">
          {error ? (
            <p role="alert" className="text-sm leading-relaxed text-ember-300">
              {error}
            </p>
          ) : null}
        </div>

        <Button type="submit" className="w-full">
          {busy
            ? mode === "signin"
              ? "Signing in…"
              : "Creating your account…"
            : mode === "signin"
              ? "Sign in"
              : "Create account"}
        </Button>
      </form>

      {/* Google, alongside the password form and never instead of it. Renders
          nothing — and loads no third-party script — when this build has no
          client id (LIBRARY.md Part 1). */}
      <GoogleSignInButton />
    </Card>
  );
}

/** How this account signs in, said plainly. Read from /v1/me's `auth_methods`
 *  so it can never disagree with what the two sign-in paths accept — an
 *  account created with Google has no password to change. */
function authMethodsLine(methods: string[] | undefined): string {
  if (!methods || methods.length === 0) return "";
  const google = methods.includes("google");
  const password = methods.includes("password");
  if (google && password) return "Signs in with Google or a password.";
  if (google) return "Signs in with Google.";
  return "Signs in with an email and password.";
}

// ---- 3. Signed in: plan / usage / upgrades ---------------------------------

function PlanCard({ user }: { user: AccountUser }) {
  const effective = effectivePlan(user);
  const lapsed = user.plan !== "free" && effective === "free";
  return (
    <Card className="p-6 sm:p-7">
      <div className="flex items-start justify-between gap-3">
        <h2 className="font-display text-lg font-semibold tracking-tight text-white">
          Plan
        </h2>
        {user.plan_status ? (
          <Badge
            tone={
              effective === "free"
                ? "neutral"
                : user.plan_status === "past_due"
                  ? "ember"
                  : "signal"
            }
          >
            {STATUS_LABELS[user.plan_status] ?? user.plan_status}
          </Badge>
        ) : null}
      </div>
      <p className="mt-4 font-display text-3xl font-semibold tracking-tight text-white">
        {PLAN_LABELS[effective]}
      </p>
      <p className="mt-2 text-sm leading-relaxed text-zinc-400">
        {user.entitlements.clips_per_month === null
          ? "Unlimited clips"
          : `${user.entitlements.clips_per_month} clips / month`}{" "}
        · up to {heightLabel(user.entitlements.max_height)} ·{" "}
        {user.entitlements.watermark_required
          ? "watermarked exports"
          : "no watermark"}
      </p>
      {lapsed ? (
        <p className="mt-3 text-xs leading-relaxed text-ember-300">
          Your {PLAN_LABELS[user.plan]} subscription is{" "}
          {(STATUS_LABELS[user.plan_status] ?? user.plan_status ?? "inactive").toLowerCase()}{" "}
          — you&rsquo;re back on Free.
        </p>
      ) : user.plan_status === "past_due" ? (
        <p className="mt-3 text-xs leading-relaxed text-ember-300">
          Payment past due — update your card via “Manage billing” to keep{" "}
          {PLAN_LABELS[user.plan]}.
        </p>
      ) : null}
    </Card>
  );
}

function UsageCard({ user }: { user: AccountUser }) {
  const { limit, used, month } = user.quota;
  const pct =
    limit === null ? 0 : limit === 0 ? 100 : Math.min(100, (used / limit) * 100);
  const exhausted = limit !== null && used >= limit;
  return (
    <Card className="p-6 sm:p-7">
      <div className="flex items-start justify-between gap-3">
        <h2 className="font-display text-lg font-semibold tracking-tight text-white">
          Usage
        </h2>
        <span className="font-mono text-xs text-zinc-500">{month}</span>
      </div>
      <p className="mt-4 flex items-baseline gap-1.5">
        <span className="font-display text-3xl font-semibold tracking-tight text-white">
          {used}
        </span>
        <span className="font-mono text-sm text-zinc-500">
          / {limit ?? "∞"} clips
        </span>
      </p>
      {limit !== null ? (
        <div
          role="progressbar"
          aria-label="Clips used this month"
          aria-valuemin={0}
          aria-valuemax={limit}
          aria-valuenow={Math.min(used, limit)}
          className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-white/10"
        >
          <div
            className={`h-full rounded-full ${
              exhausted
                ? "bg-ember-500"
                : "bg-gradient-to-r from-brand-500 to-spark-400"
            }`}
            style={{ width: `${pct}%` }}
          />
        </div>
      ) : (
        <p className="mt-4 text-xs text-zinc-500">
          No monthly cap on your plan.
        </p>
      )}
      {exhausted ? (
        <p className="mt-3 text-xs leading-relaxed text-ember-300">
          You&rsquo;ve used this month&rsquo;s clips — an upgrade below
          unlocks more.
        </p>
      ) : null}
    </Card>
  );
}

function UpgradeCard({
  user,
  busyPlan,
  onCheckout,
}: {
  user: AccountUser;
  busyPlan: string | null;
  onCheckout: (plan: Exclude<Plan, "free">) => void;
}) {
  const effective = effectivePlan(user);
  return (
    <Card className="p-6 sm:p-7">
      <h2 className="font-display text-lg font-semibold tracking-tight text-white">
        Plans
      </h2>
      <ul className="mt-2 flex flex-col divide-y divide-line">
        {PAID_PLANS.map((plan) => (
          <li
            key={plan.id}
            className="flex flex-col gap-3 py-4 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
          >
            <div>
              <p className="flex items-baseline gap-2">
                <span className="font-display text-base font-semibold tracking-tight text-white">
                  {plan.name}
                </span>
                <span className="font-mono text-xs text-zinc-500">
                  {plan.price}
                </span>
              </p>
              <p className="mt-1 text-sm text-zinc-400">{plan.blurb}</p>
            </div>
            {effective === plan.id ? (
              <Badge tone="brand" className="shrink-0 self-start sm:self-auto">
                Current plan
              </Badge>
            ) : (
              <Button
                variant="secondary"
                className="shrink-0 self-start sm:self-auto"
                onClick={() => onCheckout(plan.id)}
              >
                {busyPlan === plan.id ? "Opening checkout…" : `Get ${plan.name}`}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---- The page --------------------------------------------------------------

type CheckoutReturn = "success" | "cancelled" | null;
type ConfirmState = "idle" | "polling" | "confirmed" | "timeout";

/** ?checkout=success poll: every 2 s, ~20 s budget. */
const CONFIRM_INTERVAL_MS = 2000;
const CONFIRM_ATTEMPTS = 10;

export default function AccountApp() {
  const { user, loading, refresh, signOut } = useAccount();

  const [checkoutReturn, setCheckoutReturn] = useState<CheckoutReturn>(null);
  const [pendingPlan, setPendingPlan] = useState<"starter" | "pro" | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>("idle");
  const [billingBusy, setBillingBusy] = useState<string | null>(null);
  const [billingError, setBillingError] = useState<string | null>(null);
  const [autoNote, setAutoNote] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  // One-shot query params (?checkout=…, ?plan=…) → state, then stripped from
  // the URL so a reload or back-navigation never replays them.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const co = params.get("checkout");
    if (co === "success" || co === "cancelled") setCheckoutReturn(co);
    const plan = params.get("plan");
    if (plan === "starter" || plan === "pro") setPendingPlan(plan);
    if (co !== null || plan !== null) {
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.hash
      );
    }
  }, []);

  // 4. ?checkout=success → poll /v1/me until the webhook has landed and the
  //    effective plan is paid; past the budget, be honest about the delay.
  useEffect(() => {
    if (checkoutReturn !== "success") return;
    let cancelled = false;
    setConfirm("polling");
    void (async () => {
      for (let attempt = 0; attempt < CONFIRM_ATTEMPTS; attempt++) {
        const fresh = await refresh();
        if (cancelled) return;
        if (fresh && effectivePlan(fresh) !== "free") {
          setConfirm("confirmed");
          return;
        }
        await new Promise((r) => setTimeout(r, CONFIRM_INTERVAL_MS));
        if (cancelled) return;
      }
      setConfirm("timeout");
    })();
    return () => {
      cancelled = true;
    };
  }, [checkoutReturn, refresh]);

  const startCheckout = useCallback(
    async (plan: Exclude<Plan, "free">) => {
      setBillingError(null);
      setBillingBusy(plan);
      try {
        const { url } = await checkout(plan);
        window.location.assign(url); // busy stays set — we're leaving
      } catch (e) {
        setBillingBusy(null);
        setAutoNote(null);
        setBillingError(
          e instanceof Error ? e.message : "Checkout failed — try again."
        );
      }
    },
    []
  );

  const openPortal = useCallback(async () => {
    setBillingError(null);
    setBillingBusy("portal");
    try {
      const { url } = await portal();
      window.location.assign(url);
    } catch (e) {
      setBillingBusy(null);
      setBillingError(
        e instanceof Error
          ? e.message
          : "Couldn't open the billing portal — try again."
      );
    }
  }, []);

  // ?plan= deep link: fire checkout exactly once, as soon as we're signed in.
  // Already-paid visitors are pointed at the portal instead — a second
  // Checkout session would stack a second subscription.
  const autoCheckoutRef = useRef(false);
  useEffect(() => {
    if (!pendingPlan || loading || !user || autoCheckoutRef.current) return;
    autoCheckoutRef.current = true;
    if (effectivePlan(user) !== "free") {
      setAutoNote(
        `You're already on ${PLAN_LABELS[effectivePlan(user)]} — use "Manage billing" to change plans.`
      );
      return;
    }
    setAutoNote(`Taking you to ${PLAN_LABELS[pendingPlan]} checkout…`);
    void startCheckout(pendingPlan);
  }, [pendingPlan, loading, user, startCheckout]);

  const handleSignOut = useCallback(async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      setSigningOut(false);
    }
  }, [signingOut, signOut]);

  const view: "offline" | "loading" | "signedout" | "dashboard" = !cloudEnabled
    ? "offline"
    : loading
      ? "loading"
      : user
        ? "dashboard"
        : "signedout";

  // Move focus to the new view's heading on every state switch, so screen
  // readers and keyboard users land somewhere meaningful (skip initial mount).
  const viewRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    const heading = viewRef.current?.querySelector("h1");
    if (heading instanceof HTMLElement) heading.focus();
  }, [view]);

  // /v1/me carries no stripe_customer_id, so gate the portal button on the
  // visible Stripe signal (a plan or status was ever set); the server still
  // answers honestly if no customer exists yet.
  const hasBillingHistory =
    user !== null && (user.plan !== "free" || user.plan_status !== "");

  return (
    <section className="relative py-16 sm:py-24">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/20 blur-[120px]"
      />
      <Container className="relative">
        <div ref={viewRef} className="mx-auto w-full max-w-3xl">
          {cloudEnabled ? (
            <div aria-live="polite" className="flex flex-col gap-3 empty:hidden">
              {confirm === "polling" ? (
                <Card className="flex items-center gap-3 px-5 py-4">
                  <span
                    className="h-2 w-2 shrink-0 animate-pulse-soft rounded-full bg-brand-400"
                    aria-hidden
                  />
                  <p className="text-sm text-zinc-300">
                    Confirming your upgrade — this usually takes a few
                    seconds…
                  </p>
                </Card>
              ) : confirm === "confirmed" && user ? (
                <div className="flex items-center gap-3 rounded-2xl border border-signal-500/30 bg-signal-500/10 px-5 py-4">
                  <p className="text-sm font-medium text-white">
                    Upgrade confirmed — you&rsquo;re on{" "}
                    {PLAN_LABELS[effectivePlan(user)]}.
                  </p>
                </div>
              ) : confirm === "timeout" ? (
                <div className="flex flex-col items-start gap-3 rounded-2xl border border-ember-500/30 bg-ember-500/[0.06] px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-sm leading-relaxed text-ember-300">
                    Payment received — activating your plan can take a
                    minute. It&rsquo;ll show up here shortly.
                  </p>
                  <Button
                    variant="ghost"
                    className="shrink-0"
                    onClick={() => void refresh()}
                  >
                    Check again
                  </Button>
                </div>
              ) : null}
              {checkoutReturn === "cancelled" ? (
                <div className="flex items-center justify-between gap-3 rounded-2xl border border-line-strong bg-white/5 px-5 py-4">
                  <p className="text-sm text-zinc-300">
                    Checkout cancelled — nothing changed.
                  </p>
                  <button
                    type="button"
                    aria-label="Dismiss notice"
                    onClick={() => setCheckoutReturn(null)}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-zinc-400 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                  >
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      aria-hidden
                    >
                      <path d="M6 6l12 12M18 6 6 18" />
                    </svg>
                  </button>
                </div>
              ) : null}
              {autoNote ? (
                <p className="px-1 text-sm text-brand-300">{autoNote}</p>
              ) : null}
            </div>
          ) : null}

          {view === "offline" ? (
            <div className="animate-rise">
              <OfflineCard />
            </div>
          ) : view === "loading" ? (
            <div className="py-20 text-center" aria-busy="true">
              <p
                role="status"
                className="animate-pulse-soft text-sm text-zinc-500"
              >
                Loading your account…
              </p>
            </div>
          ) : view === "signedout" ? (
            <div className="mx-auto mt-4 max-w-md animate-rise">
              <div className="text-center">
                <Eyebrow>Account</Eyebrow>
                <h1
                  tabIndex={-1}
                  className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
                >
                  Sign in to ClipCatalyst
                </h1>
                <p className="mt-3 text-sm leading-relaxed text-zinc-400">
                  Your plan, usage, and cloud clips — one account everywhere.
                </p>
                {pendingPlan ? (
                  <p className="mt-3 text-sm text-brand-300">
                    Sign in or create an account to continue to{" "}
                    {PLAN_LABELS[pendingPlan]} checkout.
                  </p>
                ) : null}
              </div>
              <div className="mt-8">
                <AuthCard />
              </div>
            </div>
          ) : user ? (
            <div className="mt-4 animate-rise">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <Eyebrow>Account</Eyebrow>
                  <h1
                    tabIndex={-1}
                    className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
                  >
                    Your account
                  </h1>
                  <p className="mt-2 text-sm text-zinc-400">{user.email}</p>
                  {authMethodsLine(user.auth_methods) ? (
                    <p className="mt-1 font-mono text-xs text-zinc-600">
                      {authMethodsLine(user.auth_methods)}
                    </p>
                  ) : null}
                </div>
                <Button
                  variant="ghost"
                  className="self-start sm:self-auto"
                  onClick={() => void handleSignOut()}
                >
                  {signingOut ? "Signing out…" : "Sign out"}
                </Button>
              </div>

              <div className="mt-8 grid gap-5 sm:grid-cols-2">
                <PlanCard user={user} />
                <UsageCard user={user} />
              </div>

              {/* The clip library. Its own fetch (GET /v1/clips), so the
                  dashboard paints as soon as /v1/me lands. */}
              <div className="mt-10">
                <LibrarySection
                  user={user}
                  planLabel={PLAN_LABELS[effectivePlan(user)]}
                />
              </div>

              {/* Same component the Studio panel is, same local-first kit —
                  here it also syncs to the account so cloud renders match. */}
              <div className="mt-10">
                <BrandKitPanel variant="account" />
              </div>

              <div className="mt-5">
                <UpgradeCard
                  user={user}
                  busyPlan={billingBusy}
                  onCheckout={(plan) => void startCheckout(plan)}
                />
              </div>

              <div aria-live="polite">
                {billingError ? (
                  <p
                    role="alert"
                    className="mt-4 text-sm leading-relaxed text-ember-300"
                  >
                    {billingError}
                  </p>
                ) : null}
              </div>

              {hasBillingHistory ? (
                <div className="mt-6 flex flex-wrap items-center gap-3">
                  <Button variant="secondary" onClick={() => void openPortal()}>
                    {billingBusy === "portal"
                      ? "Opening billing…"
                      : "Manage billing"}
                  </Button>
                  <p className="text-xs text-zinc-500">
                    Update your card, switch plans, or cancel — via Stripe.
                  </p>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </Container>
    </section>
  );
}
