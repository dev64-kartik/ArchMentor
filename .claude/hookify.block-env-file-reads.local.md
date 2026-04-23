---
name: block-env-file-reads
enabled: true
event: bash
action: block
pattern: \b(cat|less|more|head|tail|bat|xxd|od|strings|grep|rg|ag|awk|sed|source|printenv)\b[^\n;|&]*?\.env(?![a-zA-Z0-9_-])(?!\.example)(?!\.sample)
---

🚫 **Blocked: attempt to read a `.env` file via shell**

Commands like `cat .env`, `grep FOO .env.local`, `source .env`, `head apps/api/.env` leak real secrets (JWT signers, DB creds, API keys) into the transcript and downstream logs.

**What to do instead:**
- Read the committed placeholder version: `.env.example` (this rule allows `.env.example` and `.env.sample`).
- If you need to know whether a var is set, ask the user — don't print values.
- For config surface, read the Python `Settings` models (`apps/api/.../config.py`, `apps/agent/.../config.py`) which enumerate keys without exposing values.

Reminder: `Settings` rejects `replace_with_*` placeholders at startup, so values in `.env` are real production-shape secrets. Treat `.env` as read-only-by-the-human.
