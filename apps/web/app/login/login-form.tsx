"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";

type Mode = "signin" | "signup";

function safeNext(candidate: string | null): string {
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) {
    return "/problems";
  }
  return candidate;
}

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = safeNext(searchParams.get("next"));

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setStatus(null);

    const supabase = createSupabaseBrowserClient();
    const { data, error } =
      mode === "signin"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });

    setPending(false);

    if (error) {
      setStatus(error.message);
      return;
    }

    // Sign-up with email confirmation pending: no session, stay on the page.
    if (mode === "signup" && !data.session) {
      setStatus("Account created. Check your email to confirm before signing in.");
      return;
    }

    router.push(nextPath);
    router.refresh();
  }

  function switchMode() {
    setMode(mode === "signin" ? "signup" : "signin");
    setEmail("");
    setPassword("");
    setStatus(null);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          {mode === "signin" ? "Sign in" : "Create an account"}
        </h1>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          {mode === "signin"
            ? "Use your email and password to continue."
            : "Local dev has auto-confirm enabled — you're signed in immediately."}
        </p>
      </header>

      <form onSubmit={onSubmit} className="space-y-4">
        <label className="block space-y-1.5">
          <span className="text-sm font-medium">Email</span>
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="h-10 w-full rounded-md border border-neutral-300 bg-transparent px-3 text-sm dark:border-neutral-700"
          />
        </label>

        <label className="block space-y-1.5">
          <span className="text-sm font-medium">Password</span>
          <input
            type="password"
            autoComplete={mode === "signin" ? "current-password" : "new-password"}
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-10 w-full rounded-md border border-neutral-300 bg-transparent px-3 text-sm dark:border-neutral-700"
          />
        </label>

        <Button type="submit" disabled={pending} className="w-full">
          {pending ? "Working…" : mode === "signin" ? "Sign in" : "Create account"}
        </Button>

        {status ? (
          <p className="text-sm text-red-600 dark:text-red-400">{status}</p>
        ) : null}
      </form>

      <button
        type="button"
        onClick={switchMode}
        className="self-start text-sm text-neutral-600 underline-offset-2 hover:underline dark:text-neutral-400"
      >
        {mode === "signin" ? "Need an account? Create one." : "Have an account? Sign in."}
      </button>
    </main>
  );
}
