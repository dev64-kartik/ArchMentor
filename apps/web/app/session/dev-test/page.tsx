import { redirect } from "next/navigation";

import { SessionRoom } from "@/components/livekit/session-room";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/**
 * M1 dev-only room. Lets you validate the voice loop end-to-end before
 * M2 builds `POST /sessions` to mint real session rows + rooms. The
 * room name is fixed so the agent worker can be dispatched against
 * `session-dev-test` manually. Remove once M2 ships.
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
          Joins a fixed LiveKit room named <code>session-dev-test</code>. Used to smoke-test
          the voice loop before M2 wires real session creation.
        </p>
      </header>
      <SessionRoom room="session-dev-test" />
    </main>
  );
}
