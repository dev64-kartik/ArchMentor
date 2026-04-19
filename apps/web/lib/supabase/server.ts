import { cookies } from "next/headers";
import { createServerClient } from "@supabase/ssr";

/**
 * Server-side Supabase client for use in React Server Components and
 * route handlers. Reads/writes auth cookies via `next/headers`.
 */
export async function createSupabaseServerClient() {
  const cookieStore = await cookies();
  const url = process.env["NEXT_PUBLIC_GOTRUE_URL"];
  const anonKey = process.env["NEXT_PUBLIC_SUPABASE_ANON_KEY"] ?? "anon";
  if (!url) {
    throw new Error("NEXT_PUBLIC_GOTRUE_URL is not set");
  }
  return createServerClient(url, anonKey, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: (cookiesToSet) => {
        for (const { name, value, options } of cookiesToSet) {
          cookieStore.set(name, value, options);
        }
      },
    },
  });
}
