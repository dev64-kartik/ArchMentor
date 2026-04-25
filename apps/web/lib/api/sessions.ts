import { createSupabaseBrowserClient } from "@/lib/supabase/client";

/**
 * API client for the M3 session lifecycle endpoints.
 *
 * Uses the browser-side Supabase JWT for `Authorization: Bearer ...` —
 * the API verifies via shared GoTrue JWT secret. Direct CORS rather
 * than going through a Next.js rewrite (decision in M3 plan: keep the
 * GoTrue proxy allowlist tight).
 */

const TIMEOUT_MS = 10_000;

export type ProblemSummary = {
  slug: string;
  version: number;
  title: string;
  difficulty: string;
};

export type SessionView = {
  session_id: string;
  livekit_room: string;
  livekit_url: string;
  status: "scheduled" | "active" | "ended" | "errored";
  started_at: string | null;
  ended_at: string | null;
  problem: ProblemSummary;
};

function apiUrl(): string {
  const url = process.env["NEXT_PUBLIC_API_URL"];
  if (!url) {
    throw new Error("NEXT_PUBLIC_API_URL is not set");
  }
  return url;
}

async function authHeader(): Promise<string> {
  const supabase = createSupabaseBrowserClient();
  const {
    data: { session },
    error,
  } = await supabase.auth.getSession();
  if (error) throw error;
  if (!session) throw new Error("Not authenticated");
  return `Bearer ${session.access_token}`;
}

async function fetchJson<T>(
  path: string,
  init: RequestInit & { parseAs?: (v: unknown) => v is T },
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${apiUrl()}${path}`, {
      ...init,
      credentials: "omit",
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`Request to ${path} timed out after ${TIMEOUT_MS}ms`, {
        cause: err,
      });
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${path} failed (${response.status}): ${detail}`);
  }
  const body: unknown = await response.json();
  if (init.parseAs && !init.parseAs(body)) {
    throw new Error(`${path} response is malformed`);
  }
  return body as T;
}

function isProblemSummary(value: unknown): value is ProblemSummary {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v["slug"] === "string" &&
    typeof v["version"] === "number" &&
    typeof v["title"] === "string" &&
    typeof v["difficulty"] === "string"
  );
}

function isProblemSummaryList(value: unknown): value is ProblemSummary[] {
  return Array.isArray(value) && value.every(isProblemSummary);
}

function isSessionView(value: unknown): value is SessionView {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v["session_id"] === "string" &&
    typeof v["livekit_room"] === "string" &&
    typeof v["livekit_url"] === "string" &&
    typeof v["status"] === "string" &&
    typeof v["problem"] === "object" &&
    isProblemSummary(v["problem"])
  );
}

export async function listProblems(): Promise<ProblemSummary[]> {
  const auth = await authHeader();
  return fetchJson<ProblemSummary[]>("/problems", {
    method: "GET",
    headers: { Authorization: auth },
    parseAs: isProblemSummaryList,
  });
}

export async function createSession(problemSlug: string): Promise<SessionView> {
  const auth = await authHeader();
  return fetchJson<SessionView>("/sessions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: auth,
    },
    body: JSON.stringify({ problem_slug: problemSlug }),
    parseAs: isSessionView,
  });
}

/**
 * Fire-and-forget end-session call usable from `beforeunload` (R26).
 * `keepalive: true` lets the browser commit the request even if the
 * tab is closing; supports custom headers (unlike `sendBeacon`) so
 * the existing JWT auth flow works unchanged.
 */
export function endSessionKeepalive(sessionId: string, accessToken: string): void {
  // Don't await — we want this to be fire-and-forget.
  void fetch(`${apiUrl()}/sessions/${sessionId}/end`, {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}` },
    keepalive: true,
    credentials: "omit",
  }).catch(() => undefined);
}

export async function endSession(sessionId: string): Promise<SessionView> {
  const auth = await authHeader();
  return fetchJson<SessionView>(`/sessions/${sessionId}/end`, {
    method: "POST",
    headers: { Authorization: auth },
    parseAs: isSessionView,
  });
}
