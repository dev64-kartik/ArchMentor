import { createBrowserClient } from "@supabase/ssr";

/**
 * Browser-side Supabase client. Uses the GoTrue instance exposed via
 * NEXT_PUBLIC_GOTRUE_URL. Auth tokens are stored in cookies so the API
 * can read them server-side.
 */
export function createSupabaseBrowserClient() {
  const url = process.env["NEXT_PUBLIC_GOTRUE_URL"];
  const anonKey = process.env["NEXT_PUBLIC_SUPABASE_ANON_KEY"] ?? "anon";
  if (!url) {
    throw new Error("NEXT_PUBLIC_GOTRUE_URL is not set");
  }
  return createBrowserClient(url, anonKey);
}
