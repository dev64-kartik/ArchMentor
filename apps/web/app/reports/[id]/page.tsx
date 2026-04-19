type Params = Promise<{ id: string }>;

export default async function ReportPage({ params }: { params: Params }) {
  const { id } = await params;
  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <h1 className="text-3xl font-semibold tracking-tight">Session report</h1>
      <p className="mt-4 text-neutral-600 dark:text-neutral-400">
        Per-dimension scores + timestamped evidence will render here (M5).
      </p>
      <p className="mt-2 text-xs text-neutral-400">Report {id}</p>
    </main>
  );
}
