"use client";

// Connected channels, shared (PUBLISH.md Part 4).
//
// One GET /v1/connections for everything under it: the account page's
// Connections section, and every "Post to YouTube" on a clip card in the
// library and in Studio. A per-card fetch would be a dozen identical requests
// to paint one grid, and — worse — a dozen answers that could disagree with
// each other for a second.
//
// It answers two questions the UI is not allowed to answer for itself:
// whether a platform can be CONNECTED, and whether a post to it can currently
// COMPLETE. Both are server facts (a token key, a client id, a public URL, a
// review that has landed), so this holds the server's answer and nothing else.
// PUBLISH.md's rule — the UI never offers a platform that cannot finish a post
// — is enforceable only because the answer arrives from one place.
//
// Signed out, or with no cloud API, it settles to null with no network at all,
// exactly as AccountProvider does: there are no connections without an
// account, and nothing downstream renders a control that would need one.

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
import { cloudEnabled } from "@/components/studio/cloud";
import {
  AccountError,
  listConnections,
  type ConnectionList,
} from "@/lib/account";

type ConnectionsContextValue = {
  /** The server's snapshot, or null (signed out, API unset, or not loaded). */
  connections: ConnectionList | null;
  /** True until the first round-trip settles (or is skipped). */
  loading: boolean;
  /** A failed load, in the server's own words; null while it is fine. */
  error: string | null;
  /** Re-read /v1/connections. Resolves with the fresh snapshot, or null when
   *  there is nothing to read. Never throws — it reports through `error`. */
  refresh: () => Promise<ConnectionList | null>;
};

const ConnectionsContext = createContext<ConnectionsContextValue>({
  connections: null,
  loading: false,
  error: null,
  refresh: async () => null,
});

export function useConnections(): ConnectionsContextValue {
  return useContext(ConnectionsContext);
}

export default function ConnectionsProvider({
  /** Whether there is a session to read connections with. AccountProvider
   *  passes it down rather than this calling `useAccount()`: the provider
   *  renders INSIDE that context, and importing it back would tie the two
   *  files into a cycle for one boolean. */
  signedIn,
  children,
}: {
  signedIn: boolean;
  children: ReactNode;
}) {
  const [connections, setConnections] = useState<ConnectionList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const deadRef = useRef(false);

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
    };
  }, []);

  const refresh = useCallback(async (): Promise<ConnectionList | null> => {
    if (!cloudEnabled || !signedIn) {
      setConnections(null);
      setError(null);
      return null;
    }
    setLoading(true);
    try {
      const fresh = await listConnections();
      if (deadRef.current) return fresh;
      setConnections(fresh);
      setError(null);
      return fresh;
    } catch (e) {
      if (deadRef.current) return null;
      // A 401 means the session went between /v1/me and this call.
      // AccountProvider owns that transition; here it is simply "nothing to
      // show", not an error worth putting on screen.
      if (e instanceof AccountError && e.status === 401) {
        setConnections(null);
        setError(null);
        return null;
      }
      setError(
        e instanceof Error
          ? e.message
          : "Couldn't load your connected channels. Try again."
      );
      return null;
    } finally {
      if (!deadRef.current) setLoading(false);
    }
  }, [signedIn]);

  // Signing in loads them; signing out drops them. Re-running on `signedIn`
  // rather than on a user object keeps this to the two transitions that
  // actually change the answer.
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ connections, loading, error, refresh }),
    [connections, loading, error, refresh]
  );

  return (
    <ConnectionsContext.Provider value={value}>
      {children}
    </ConnectionsContext.Provider>
  );
}
