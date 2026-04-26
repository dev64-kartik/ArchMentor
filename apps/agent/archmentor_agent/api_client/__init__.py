"""HTTP client modules for the control-plane API.

These clients are thin wrappers around httpx that authenticate via
X-Agent-Token and follow the same retry-with-backoff discipline as
`ledger.client` and `snapshots.client`.
"""
