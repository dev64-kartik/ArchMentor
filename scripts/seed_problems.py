"""Seed the `problems` table from `packages/problems/*.yaml`.

Implementation lands in M6. Will:
1. Walk `packages/problems/*.yaml`.
2. Validate each against the problem schema (see `scripts/validate_problem.py`).
3. Upsert into Postgres, bumping `version` on content changes.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit("seed_problems.py is not implemented yet — lands in M6.")


if __name__ == "__main__":
    main()
