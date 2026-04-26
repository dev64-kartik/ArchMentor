import { redirect } from "next/navigation";

import { StartSessionForm } from "@/components/session/start-session-form";
import { isProblemSummaryList, type ProblemSummary } from "@/lib/api/sessions";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Server-rendered problem catalog + start-session form.
 *
 * The catalog fetch runs server-side (not in the client form) so the
 * Supabase access token never has to round-trip through a Server
 * Action. The candidate sees the catalog instantly on first paint
 * even on a slow connection.
 */
export default async function NewSessionPage() {
  const supabase = await createSupabaseServerClient();
  const {
    data: { session },
    error,
  } = await supabase.auth.getSession();
  if (error || !session) {
    redirect("/login?next=/session/new");
  }

  const apiUrl = process.env["NEXT_PUBLIC_API_URL"];
  if (!apiUrl) {
    throw new Error("NEXT_PUBLIC_API_URL is not set");
  }

  let problems: ProblemSummary[] = [];
  let fetchError: string | null = null;
  try {
    const response = await fetch(`${apiUrl}/problems`, {
      headers: { Authorization: `Bearer ${session.access_token}` },
      cache: "no-store",
    });
    if (!response.ok) {
      fetchError = `Catalog request failed (${response.status})`;
    } else {
      const body: unknown = await response.json();
      if (isProblemSummaryList(body)) {
        problems = body;
      } else {
        fetchError = "Catalog response is malformed";
      }
    }
  } catch (err) {
    fetchError = err instanceof Error ? err.message : String(err);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Start a new session</h1>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Pick a problem to discuss with the AI interviewer. You&apos;ll have ~45 minutes to
          walk through your design on the whiteboard.
        </p>
      </header>
      {fetchError ? (
        <div
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
        >
          {fetchError}
        </div>
      ) : (
        <StartSessionForm problems={problems} />
      )}
    </main>
  );
}
