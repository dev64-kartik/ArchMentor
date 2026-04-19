# @archmentor/web

Next.js 15 frontend for ArchMentor.

## Stack

- Next.js 15 (App Router) + React 19 + TypeScript 5
- Tailwind 4 (CSS-first config via `@theme`)
- shadcn/ui (`components.json` — run `pnpm dlx shadcn@latest add <component>` as needed)
- oxlint (lint + auto-fix)
- Supabase SSR (`@supabase/ssr`)
- LiveKit client (`livekit-client`)
- Vitest

## Commands

```bash
pnpm install                    # from repo root
pnpm --filter @archmentor/web dev
pnpm --filter @archmentor/web build
pnpm --filter @archmentor/web lint
pnpm --filter @archmentor/web typecheck
pnpm --filter @archmentor/web test
```

## Env

Local env comes from `.env.local` (not committed). Public vars must be
prefixed with `NEXT_PUBLIC_`:

- `NEXT_PUBLIC_API_URL` — FastAPI base URL
- `NEXT_PUBLIC_GOTRUE_URL` — Supabase Auth (GoTrue)
- `NEXT_PUBLIC_LIVEKIT_URL` — LiveKit WebSocket URL
- `NEXT_PUBLIC_SUPABASE_ANON_KEY` — (optional) anon key; defaults to `"anon"` in dev

## Routes

- `/` — landing
- `/problems` — catalog (M3+)
- `/session/[id]` — live session (M1, M2, M3)
- `/reports/[id]` — feedback report (M5)

## oxfmt

`oxfmt` (dedicated formatter) is pre-GA. Today we lean on `oxlint --fix`;
migrate to `oxfmt` once it reaches stable.
