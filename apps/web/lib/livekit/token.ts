import { createSupabaseBrowserClient } from "@/lib/supabase/client";

export type LiveKitToken = {
  token: string;
  url: string;
  room: string;
  identity: string;
};

const TOKEN_FETCH_TIMEOUT_MS = 10_000;

function isLiveKitToken(value: unknown): value is LiveKitToken {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate["token"] === "string" &&
    typeof candidate["url"] === "string" &&
    typeof candidate["room"] === "string" &&
    typeof candidate["identity"] === "string"
  );
}

/**
 * Fetch a LiveKit room token from the control-plane API. The API
 * verifies the caller's Supabase JWT and mints a 15-min room-scoped
 * LiveKit token bound to the user's id.
 *
 * Uses `getUser()` (server-validated) rather than `getSession()` so
 * that a revoked or disabled account can't keep minting LiveKit
 * tokens from the cached local JWT until expiry.
 */
export async function fetchLiveKitToken(room: string): Promise<LiveKitToken> {
  const apiUrl = process.env["NEXT_PUBLIC_API_URL"];
  if (!apiUrl) {
    throw new Error("NEXT_PUBLIC_API_URL is not set");
  }

  const supabase = createSupabaseBrowserClient();
  const { data: userData, error: userError } = await supabase.auth.getUser();
  if (userError) {
    throw userError;
  }
  if (!userData.user) {
    throw new Error("Not authenticated");
  }
  const {
    data: { session },
    error: sessionError,
  } = await supabase.auth.getSession();
  if (sessionError) {
    throw sessionError;
  }
  if (!session) {
    throw new Error("Not authenticated");
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TOKEN_FETCH_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${apiUrl}/livekit/token`, {
      method: "POST",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify({ room }),
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(
        `LiveKit token request timed out after ${TOKEN_FETCH_TIMEOUT_MS}ms`,
        { cause: err },
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`LiveKit token request failed (${response.status}): ${detail}`);
  }

  const body: unknown = await response.json();
  if (!isLiveKitToken(body)) {
    throw new Error("LiveKit token response is malformed");
  }
  return body;
}
