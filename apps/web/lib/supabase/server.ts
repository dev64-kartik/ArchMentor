import { cookies } from "next/headers";
import { createServerClient } from "@supabase/ssr";

/**
 * Server-side Supabase client for use in React Server Components and
 * route handlers. Reads/writes auth cookies via `next/headers`.
 *
 * Next.js 15 forbids cookie mutation outside Server Actions and Route
 * Handlers. The `setAll` callback runs during token rotation inside
 * `auth.getUser()`, which Server Components call during render — so the
 * write must be best-effort; middleware handles the durable refresh.
 */
export async function createSupabaseServerClient() {
  const cookieStore = await cookies();
  // Use the same URL on server and browser so Supabase-SSR derives the
  // same cookie storage key on both sides — otherwise server reads miss
  // the session the browser just set.
  const url = process.env["NEXT_PUBLIC_GOTRUE_URL"];
  const anonKey = process.env["NEXT_PUBLIC_SUPABASE_ANON_KEY"] ?? "anon";
  if (!url) {
    throw new Error("NEXT_PUBLIC_GOTRUE_URL is not set");
  }
  return createServerClient(url, anonKey, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: (cookiesToSet) => {
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // Server Component render context — cookie mutation is forbidden.
          // Middleware owns the durable refresh on the next request.
        }
      },
    },
  });
}
