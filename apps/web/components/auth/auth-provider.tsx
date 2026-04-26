"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  type ReactNode,
} from "react";

import { createSupabaseBrowserClient } from "@/lib/supabase/client";

type AuthContextValue = {
  tokenRef: React.RefObject<string | null>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Top-level provider that holds a synchronously-readable ref to the
 * current Supabase access token. Kept fresh via `onAuthStateChange`
 * so `beforeunload` handlers can read the token without awaiting
 * `getSession()` (which the browser does not allow to complete
 * after `beforeunload` returns).
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const tokenRef = useRef<string | null>(null);
  // Stable context value — wraps the ref so the object identity never
  // changes between renders, avoiding spurious consumer re-renders.
  const ctxRef = useRef<AuthContextValue>({ tokenRef });

  useEffect(() => {
    const supabase = createSupabaseBrowserClient();

    // Seed the ref synchronously-ish on mount.
    void supabase.auth.getSession().then(({ data: { session } }) => {
      tokenRef.current = session?.access_token ?? null;
    });

    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        tokenRef.current = session?.access_token ?? null;
      },
    );

    return () => {
      listener.subscription.unsubscribe();
    };
  }, []);

  return (
    <AuthContext.Provider value={ctxRef.current}>
      {children}
    </AuthContext.Provider>
  );
}

/**
 * Returns a ref whose `.current` is always the latest Supabase access
 * token (or `null` when signed out). Safe to read synchronously inside
 * `beforeunload` handlers.
 *
 * Must be called inside `<AuthProvider>`.
 */
export function useAccessTokenRef(): React.RefObject<string | null> {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAccessTokenRef must be used inside <AuthProvider>");
  }
  return ctx.tokenRef;
}
