"""Canvas: Excalidraw scene → fenced text + snapshot ingest client.

Public API:
- `parse_scene(scene)` — convert scene JSON to a multi-section text
  description with `<label>` fencing and an 8 KiB UTF-8 byte cap.
- `CanvasSnapshotClient` — fire-and-forget HTTP client for the
  canvas-snapshots ingest route. Mirrors `SnapshotClient`.
"""

from archmentor_agent.canvas.client import CanvasSnapshotClient, CanvasSnapshotClientConfig
from archmentor_agent.canvas.parser import parse_scene

__all__ = [
    "CanvasSnapshotClient",
    "CanvasSnapshotClientConfig",
    "parse_scene",
]
