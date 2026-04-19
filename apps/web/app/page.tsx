import Link from "next/link";

export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-6 px-6">
      <h1 className="text-4xl font-semibold tracking-tight">ArchMentor</h1>
      <p className="text-lg text-neutral-600 dark:text-neutral-400">
        Practice system design interviews with an AI interviewer who listens, watches your
        whiteboard, and gives rubric-anchored feedback.
      </p>
      <div className="flex gap-3">
        <Link
          href="/problems"
          className="rounded-md bg-neutral-900 px-4 py-2 text-white hover:bg-neutral-800 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-200"
        >
          Browse problems
        </Link>
      </div>
    </main>
  );
}
