import Link from "next/link";

export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-6 px-6">
      <h1 className="text-4xl font-semibold tracking-tight">ArchMentor</h1>
      <p className="text-lg text-neutral-600 dark:text-neutral-400">
        Practice system design interviews with an AI interviewer who listens, watches your
        whiteboard, and gives rubric-anchored feedback.
      </p>
      <div className="flex flex-wrap gap-3">
        <Link
          href="/session/new"
          className="rounded-md bg-emerald-600 px-4 py-2 text-white hover:bg-emerald-700"
        >
          Start a session
        </Link>
        <Link
          href="/problems"
          className="rounded-md border border-neutral-300 px-4 py-2 text-neutral-900 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-100 dark:hover:bg-neutral-800"
        >
          Browse problems
        </Link>
      </div>
    </main>
  );
}
