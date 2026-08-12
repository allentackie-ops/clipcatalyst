"use client";

// Account context: one GET /v1/me shared by everything under it (the account
// page, Studio). Fetches on mount when a session token exists; a 401 clears
// the token (lib/account does that) and settles to signed-out. With
// NEXT_PUBLIC_CLOUD_API unset it settles immediately — no network at all.
//
// It also mounts ConnectionsProvider (PUBLISH.md Part 4) around the same
// children, so a page that has an account also has its connected channels.
// The nesting is deliberate: connections only exist for a session, so the
// thing that knows whether there is one is what decides whether to look.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ConnectionsProvider from "@/components/publish/ConnectionsProvider";
import { cloudEnabled } from "@/components/studio/cloud";
import {
  AccountError,
  getToken,
  logout,
  me,
  type AccountUser,
} from "@/lib/account";

type AccountContextValue = {
  /** The signed-in user per /v1/me, or null (signed out / API unset). */
  user: AccountUser | null;
  /** True until the first /v1/me round-trip settles (or is skipped). */
  loading: boolean;
  /** Re-fetch /v1/me. Resolves with the fresh user; null = signed out; the
   *  last known user on a transient failure (never throws). */
  refresh: () => Promise<AccountUser | null>;
  /** Revoke the session server-side and forget it locally. */
  signOut: () => Promise<void>;
};

const AccountContext = createContext<AccountContextValue>({
  user: null,
  loading: false,
  refresh: async () => null,
  signOut: async () => {},
});

export function useAccount(): AccountContextValue {
  return useContext(AccountContext);
}

export default function AccountProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [user, setUser] = useState<AccountUser | null>(null);
  const [loading, setLoading] = useState(cloudEnabled);
  // Mirror of `user` so refresh() can return the last known value without
  // depending on (and re-creating itself for) every user change.
  const userRef = useRef<AccountUser | null>(null);

  const apply = useCallback((next: AccountUser | null) => {
    userRef.current = next;
    setUser(next);
  }, []);

  const refresh = useCallback(async (): Promise<AccountUser | null> => {
    if (!cloudEnabled || getToken() === null) {
      apply(null);
      return null;
    }
    try {
      const fresh = await me();
      apply(fresh);
      return fresh;
    } catch (e) {
      if (e instanceof AccountError && e.status === 401) {
        // Session expired or revoked — the stale token is already cleared.
        apply(null);
        return null;
      }
      // Transient failure (network, 5xx): keep the last known user instead
      // of flickering the whole UI to signed-out on a blip.
      return userRef.current;
    }
  }, [apply]);

  useEffect(() => {
    if (!cloudEnabled) return;
    void refresh().finally(() => setLoading(false));
  }, [refresh]);

  const signOut = useCallback(async () => {
    await logout();
    apply(null);
  }, [apply]);

  const value = useMemo(
    () => ({ user, loading, refresh, signOut }),
    [user, loading, refresh, signOut]
  );

  return (
    <AccountContext.Provider value={value}>
      <ConnectionsProvider signedIn={user !== null}>
        {children}
      </ConnectionsProvider>
    </AccountContext.Provider>
  );
}
