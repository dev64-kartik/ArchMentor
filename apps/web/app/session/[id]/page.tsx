import { redirect } from "next/navigation";

import { SessionShell } from "@/components/session/session-shell";
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

  return <SessionShell sessionId={id} roomName={roomName} />;
}
