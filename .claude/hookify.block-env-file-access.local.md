---
name: block-env-file-access
enabled: true
event: file
tool_matcher: Edit|Write|MultiEdit|Read
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: (^|/)\.env(\.(?!example$|sample$)[\w.-]+|$)
---

🚫 **Blocked: attempt to read or edit a real `.env` file**

`.env`, `.env.local`, `.env.production`, etc. hold secrets (JWT signers, DB creds, API keys) and must not be touched by the agent. `.env.example` and `.env.sample` are allowed — they hold only `replace_with_*` placeholders.

**What to do instead:**
- For reading: use `.env.example` to learn which keys exist; for values, ask the user.
- For writing: ask the user to edit their `.env` themselves. If a new variable is needed, update `.env.example` with a `replace_with_<name>` placeholder and tell the user what to set.
- Never paste real secrets into any file the agent writes.

Relevant project rules (`CLAUDE.md`): `API_JWT_SECRET` must equal `GOTRUE_JWT_SECRET`; `Settings` rejects any value containing the `replace_with_` marker — do not "helpfully" substitute real values into `.env.example`.
