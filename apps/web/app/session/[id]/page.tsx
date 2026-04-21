import { redirect } from "next/navigation";

import { SessionRoom } from "@/components/livekit/session-room";
import { createSupabaseServerClient } from "@/lib/supabase/server";

type Params = Promise<{ id: string }>;

export default async function SessionPage({ params }: { params: Params }) {
  const { id } = await params;
  const supabase = await createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    redirect(`/login?next=/session/${id}`);
  }

  const roomName = `session-${id}`;

  return (
    <main className="grid h-screen grid-cols-[70%_30%]">
      <section className="border-r border-neutral-200 p-4 dark:border-neutral-800">
        <p className="text-sm text-neutral-500">Excalidraw canvas mounts here (M3).</p>
        <p className="text-xs text-neutral-400">Session {id}</p>
      </section>
      <aside className="flex flex-col gap-4 p-4">
        <SessionRoom room={roomName} />
        <div className="rounded-md border border-dashed border-neutral-300 p-3 text-xs text-neutral-500 dark:border-neutral-700">
          Transcript + phase indicator land in M2/M4.
        </div>
      </aside>
    </main>
  );
}
