"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { createSession, type ProblemSummary } from "@/lib/api/sessions";

type Props = {
  problems: ProblemSummary[];
};

/**
 * Client component for the `/session/new` flow.
 *
 * Renders a problem picker + consent text, posts to `/sessions`, and
 * navigates to `/session/{id}` on success. Catalog fetch happens in
 * the parent server component so the JWT bearer is the only auth
 * surface the client touches.
 */
export function StartSessionForm({ problems }: Props) {
  const router = useRouter();
  const [selectedSlug, setSelectedSlug] = useState<string>(
    problems[0]?.slug ?? "",
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedSlug || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(selectedSlug);
      router.push(`/session/${session.session_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  if (problems.length === 0) {
    return (
      <div
        role="alert"
        className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
      >
        No problems are available right now. Try again later.
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-6">
      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium">Choose a problem</legend>
        {problems.map((problem) => (
          <label
            key={problem.slug}
            className={`flex cursor-pointer items-start gap-3 rounded-md border p-4 transition-colors ${
              selectedSlug === problem.slug
                ? "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/20"
                : "border-neutral-200 hover:border-neutral-400 dark:border-neutral-800 dark:hover:border-neutral-600"
            }`}
          >
            <input
              type="radio"
              name="problem"
              value={problem.slug}
              checked={selectedSlug === problem.slug}
              onChange={() => setSelectedSlug(problem.slug)}
              className="mt-1"
            />
            <div className="flex flex-col gap-1">
              <span className="font-medium">{problem.title}</span>
              <span className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                {problem.difficulty}
              </span>
            </div>
          </label>
        ))}
      </fieldset>

      <p className="text-xs text-neutral-600 dark:text-neutral-400">
        By starting a session you consent to your microphone audio and
        whiteboard activity being processed by an AI interviewer for the
        duration of the session.
      </p>

      <button
        type="submit"
        disabled={!selectedSlug || submitting}
        className="self-start rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {submitting ? "Starting…" : "Start session"}
      </button>

      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}
