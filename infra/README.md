# infra — local dev stack

Docker Compose stack for ArchMentor local development.

## Services

| Service | Purpose | Port(s) |
|---|---|---|
| `postgres` | Shared Postgres 16 (dbs: `archmentor`, `auth`, `langfuse`) | 5432 |
| `gotrue` | Supabase Auth (GoTrue) | 9999 |
| `redis` | Hot session state | 6379 |
| `minio` | Object storage (audio, canvas snapshots) | 9000 (S3), 9001 (console) |
| `livekit` | WebRTC signalling + media | 7880 (ws), 7881 (tcp), 7882/udp |
| `langfuse` | LLM observability | 3001 |

## Usage

From repo root:

```bash
cp .env.example .env         # fill in secrets
./scripts/dev.sh             # boots the full stack
```

Teardown:

```bash
docker compose -f infra/docker-compose.yml down          # stop, keep volumes
docker compose -f infra/docker-compose.yml down -v       # stop, wipe data
```

## Version pins

Image tags in `docker-compose.yml` are major-version pins for scaffolding.
Before first boot, verify current stable tags at each image's registry and
re-pin to exact versions. Commit the updated tags.

## Secrets

`.env` is gitignored. The repo ships `.env.example` only. For all
JWT/NEXTAUTH/SALT values, generate a fresh 32+ char string:

```bash
openssl rand -base64 48
```

`API_JWT_SECRET` must match `GOTRUE_JWT_SECRET` — the API verifies Supabase
Auth tokens locally using that shared secret.
