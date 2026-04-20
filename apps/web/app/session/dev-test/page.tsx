import { redirect } from "next/navigation";

import { SessionRoom } from "@/components/livekit/session-room";
import { createSupabaseServerClient } from "@/lib/supabase/server";

// Matches scripts/seed_dev_session.py — the room name embeds this UUID so
// the agent can extract it via _session_id_from_ctx and write events.
const DEV_ROOM = "session-00000000-0000-0000-0000-000000000001";

/**
 * M1 dev-only room. Lets you validate the voice loop end-to-end before
 * M2 builds `POST /sessions` to mint real session rows + rooms. Run
 * `uv run python scripts/seed_dev_session.py` once before using.
 */
export default async function DevTestSessionPage() {
  const supabase = await createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    redirect("/login?next=/session/dev-test");
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">M1 dev room</h1>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Joins <code>{DEV_ROOM}</code>. Run{" "}
          <code>uv run python scripts/seed_dev_session.py</code> once before using so the agent
          has a session row to append events against.
        </p>
      </header>
      <SessionRoom room={DEV_ROOM} />
    </main>
  );
}
