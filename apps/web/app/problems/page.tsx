import { redirect } from "next/navigation";

import { SignOutButton } from "@/components/auth/sign-out-button";
import { createSupabaseServerClient } from "@/lib/supabase/server";

export default async function ProblemsPage() {
  const supabase = await createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    redirect("/login?next=/problems");
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <header className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Problems</h1>
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Signed in as <span className="font-medium">{user.email ?? "unknown"}</span>
          </p>
        </div>
        <SignOutButton />
      </header>
      <p className="mt-8 text-neutral-600 dark:text-neutral-400">
        Problem catalog will load here. Seeded in M6.
      </p>
    </main>
  );
}
