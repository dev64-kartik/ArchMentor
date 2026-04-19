type Params = Promise<{ id: string }>;

export default async function SessionPage({ params }: { params: Params }) {
  const { id } = await params;
  return (
    <main className="grid h-screen grid-cols-[70%_30%]">
      <section className="border-r border-neutral-200 p-4 dark:border-neutral-800">
        <p className="text-sm text-neutral-500">Excalidraw canvas mounts here (M3).</p>
        <p className="text-xs text-neutral-400">Session {id}</p>
      </section>
      <aside className="flex flex-col p-4">
        <p className="text-sm text-neutral-500">Transcript + phase indicator (M1, M4).</p>
      </aside>
    </main>
  );
}
