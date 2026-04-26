"""Local `ProblemCard` source for M2's dev-test flow.

M2 ships without a `POST /sessions` endpoint or a bootstrap API route
(both land in M3). The agent worker therefore can't fetch the candidate's
`Problem` row from the control plane at `on_enter`. Instead, the agent
synthesizes a `ProblemCard` in-process from the constants defined here
and `scripts/seed_dev_session.py` reads the same constants so the
Postgres row and the in-memory card stay byte-identical.

When M3 lands `POST /sessions` with a real bootstrap route, replace
`load_problem_card` with an HTTP call and delete this file — the seed
script will keep the shared strings if they're still useful for tests.

The URL-shortener problem + rubric are ≤ 800 words total (plan's Unit 10
budget) so the cached system block stays under Opus 4.x's ~4096-token
cache-activation floor without padding. Re-measure with
`anthropic.messages.count_tokens` if the problem grows.
"""

from __future__ import annotations

from archmentor_agent.state.session_state import ProblemCard

DEV_PROBLEM_SLUG = "dev-test"
DEV_PROBLEM_VERSION = 2
DEV_PROMPT_VERSION = "m3-canvas"

DEV_PROBLEM_TITLE = "Design URL Shortener"

DEV_PROBLEM_STATEMENT_MD = """\
Design a URL-shortening service (think bit.ly). The service maps long
URLs to short 7-character codes; when a user hits a short URL we
redirect them to the original.

Scope:
- 100M new short URLs created per month (≈40 writes/sec avg).
- 10B redirect requests per month (≈4000 reads/sec avg, 10x peak).
- URLs never change after creation; short-to-long is append-only.
- Users should be able to see their own historical short URLs.
- Custom aliases (user-chosen codes) are in scope.
- Analytics (click counts per short code) is in scope but low priority.

Out of scope: rate limiting of the public redirect endpoint, abuse
detection (phishing URL filtering), paid plans, multi-region DR.

Walk me through how you'd build this. Start wherever feels natural.
"""

DEV_PROBLEM_RUBRIC_YAML = """\
dimensions:
  - name: functional_requirements
    description: Clarifies read/write split, custom alias, analytics scope.
    depth_levels:
      shallow: names reads + writes without numbers
      solid: attaches capacity numbers and distinguishes hot vs cold paths
      thorough: surfaces custom-alias collision handling + analytics write path

  - name: capacity_estimation
    description: Derives QPS, storage footprint, and bandwidth from the spec.
    depth_levels:
      shallow: handwaves "a lot of reads" without a number
      solid: computes read QPS, write QPS, and 5-year storage from scope
      thorough: projects peak QPS, CDN offload, and index growth

  - name: storage_design
    description: Picks datastore, schema, and explains the short-code index.
    depth_levels:
      shallow: names a database without reasoning about access pattern
      solid: chooses KV vs relational with access-pattern justification
      thorough: covers sharding strategy + hot-key handling + TTL policy

  - name: hot_path_design
    description: Explains the redirect path end-to-end at read scale.
    depth_levels:
      shallow: describes only the DB lookup
      solid: adds a cache layer with hit-rate reasoning
      thorough: covers CDN, cache warming, and failure modes

  - name: tradeoffs
    description: Surfaces alternatives and why they were rejected.
    depth_levels:
      shallow: mentions one alternative superficially
      solid: contrasts two approaches with concrete drawbacks each
      thorough: ties tradeoff back to requirements (durability, latency, cost)
"""


def build_dev_problem_card() -> ProblemCard:
    """Return the ProblemCard the agent hands to the brain at `on_enter`.

    Pure function — no I/O. The seed script imports these constants
    directly rather than calling this builder so the insertion path
    stays obvious at review time.
    """
    return ProblemCard(
        slug=DEV_PROBLEM_SLUG,
        version=DEV_PROBLEM_VERSION,
        title=DEV_PROBLEM_TITLE,
        statement_md=DEV_PROBLEM_STATEMENT_MD,
        rubric_yaml=DEV_PROBLEM_RUBRIC_YAML,
    )


__all__ = [
    "DEV_PROBLEM_RUBRIC_YAML",
    "DEV_PROBLEM_SLUG",
    "DEV_PROBLEM_STATEMENT_MD",
    "DEV_PROBLEM_TITLE",
    "DEV_PROBLEM_VERSION",
    "DEV_PROMPT_VERSION",
    "build_dev_problem_card",
]
