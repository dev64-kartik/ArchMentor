import { createSupabaseBrowserClient } from "@/lib/supabase/client";

export type LiveKitToken = {
  token: string;
  url: string;
  room: string;
  identity: string;
};

/**
 * Fetch a LiveKit room token from the control-plane API. The API
 * verifies the caller's Supabase JWT and mints a 15-min room-scoped
 * LiveKit token bound to the user's id.
 */
export async function fetchLiveKitToken(room: string): Promise<LiveKitToken> {
  const apiUrl = process.env["NEXT_PUBLIC_API_URL"];
  if (!apiUrl) {
    throw new Error("NEXT_PUBLIC_API_URL is not set");
  }

  const supabase = createSupabaseBrowserClient();
  const {
    data: { session },
    error,
  } = await supabase.auth.getSession();
  if (error) {
    throw error;
  }
  if (!session) {
    throw new Error("Not authenticated");
  }

  const response = await fetch(`${apiUrl}/livekit/token`, {
    method: "POST",
    credentials: "omit",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${session.access_token}`,
    },
    body: JSON.stringify({ room }),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`LiveKit token request failed (${response.status}): ${detail}`);
  }

  return (await response.json()) as LiveKitToken;
}
