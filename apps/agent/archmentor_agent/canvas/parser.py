"""Excalidraw scene → fenced-text description for the brain prompt.

The parser converts Excalidraw scene JSON into a compact multi-line
string with four sections — `Components:`, `Connections:`,
`Annotations:`, `Unnamed shapes:` — that the brain can read alongside
the rolling transcript. Three concerns shape the output:

1. **Label fencing.** Every text label produced by the candidate is
   wrapped in `<label>...</label>` with inner `<` / `>` HTML-escaped.
   The brain prompt's `[Canvas]` clause instructs the model to treat
   fenced content as quoted, untrusted input. Mitigation, not defense
   — defense-in-depth is M6+.

2. **8 KiB UTF-8 byte cap.** Output is truncated at a UTF-8-safe
   boundary with a `[truncated — N more …]` marker. The brain's input
   tokens are billed; an unbounded canvas description would let the
   candidate balloon a single turn's input cost.

3. **Image elements never leak data.** `image` elements render as
   `<label>[embedded image]</label>` placeholders. M3 has no
   vision/OCR; the candidate sees the image locally, the brain sees a
   placeholder. The disclosure overlay (R19) makes this expectation
   visible to the candidate.

Pure function. No I/O, no logging — the caller writes the
`canvas_change` ledger event with `parsed_text` (R21).
"""

from __future__ import annotations

import html
from typing import Any, cast

# Total UTF-8 byte budget for the whole output. Sized to fit comfortably
# within a single brain turn's input window without crowding out the
# rolling transcript. Tuning happens via prompt iteration in M4+.
_OUTPUT_CAP_BYTES = 8 * 1024

# Element types the parser explicitly recognises. Anything else falls
# through into "Unnamed shapes" so unknown / future Excalidraw types
# don't crash the parser.
_RECT_LIKE_TYPES = frozenset({"rectangle", "ellipse", "diamond"})
_TEXT_TYPE = "text"
_ARROW_LIKE_TYPES = frozenset({"arrow", "line"})
_IMAGE_TYPE = "image"

# Sentinel labels for arrows whose binding doesn't resolve. Both are
# wrapped in `<label>...</label>` so the brain prompt's trust contract
# is uniform: every parenthesised value inside a connection line is
# fenced, regardless of whether it came from the candidate or the
# server. `(unbound)` means the arrow has no `startBinding` /
# `endBinding` at all. `(unresolved)` means the binding points at an
# element id that doesn't exist in the current scene — usually a stale
# reference after deletion.
_UNBOUND = "<label>(unbound)</label>"
_UNRESOLVED = "<label>(unresolved)</label>"

_IMAGE_PLACEHOLDER = "[embedded image]"


def parse_scene(scene: dict[str, Any]) -> str:
    """Return a fenced multi-line description of the Excalidraw scene.

    `scene["elements"]` is a list of element dicts. Other keys (notably
    `appState`, `files`) are ignored — `files` is stripped upstream by
    the agent's canvas handler (R17 server-side enforcement).
    """
    elements = list(scene.get("elements", []))
    if not isinstance(elements, list):
        # Defensive: an upstream caller passed a non-list; treat as empty.
        elements = []

    label_index = _build_label_index(elements)

    components: list[str] = []
    connections: list[str] = []
    annotations: list[str] = []
    unnamed: list[str] = []

    seen_text_ids: set[str] = set(label_index.values_to_text_ids())

    for element in elements:
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        element_id = element.get("id", "")

        if element_type in _RECT_LIKE_TYPES:
            label = label_index.label_for(element_id)
            if label is not None:
                components.append(f"<label>{_escape(label)}</label>")
            else:
                unnamed.append(_position_hint(element, element_type))

        elif element_type in _ARROW_LIKE_TYPES:
            connections.append(_format_arrow(element, label_index))

        elif element_type == _TEXT_TYPE:
            # A text element is either a label (already consumed via
            # containerId binding) or a free-floating annotation.
            if element_id in seen_text_ids:
                continue
            text_value = element.get("text", "")
            if isinstance(text_value, str) and text_value.strip():
                annotations.append(f"<label>{_escape(text_value)}</label>")

        elif element_type == _IMAGE_TYPE:
            unnamed.append(f"<label>{_escape(_IMAGE_PLACEHOLDER)}</label>")

        else:
            # Unknown / future element type — render as an unnamed shape
            # rather than raising, so adversarial or version-skewed
            # scenes still produce a bounded output.
            type_str = str(element_type) if element_type is not None else "unknown"
            unnamed.append(_position_hint(element, type_str))

    rendered = _render_sections(components, connections, annotations, unnamed)
    return _truncate_to_byte_cap(rendered, _OUTPUT_CAP_BYTES)


# ---------- internals ----------


class _LabelIndex:
    """Resolves element ids to their human-readable labels.

    A label can come from two places:
    - A `text` element with `containerId == element_id` (the common case
      for shapes; Excalidraw stores rectangle labels as bound text
      elements).
    - An inline `label.text` field on arrow elements.

    The index also tracks which text-element ids have been consumed as
    container labels so the caller can skip them when emitting the
    `Annotations` section.
    """

    def __init__(self, container_to_text: dict[str, str], text_id_to_container: dict[str, str]):
        self._container_to_text = container_to_text
        self._text_id_to_container = text_id_to_container

    def label_for(self, element_id: str) -> str | None:
        if not element_id:
            return None
        return self._container_to_text.get(element_id)

    def values_to_text_ids(self) -> list[str]:
        return list(self._text_id_to_container.keys())


def _build_label_index(elements: list[Any]) -> _LabelIndex:
    container_to_text: dict[str, str] = {}
    text_id_to_container: dict[str, str] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("type") != _TEXT_TYPE:
            continue
        container_id = element.get("containerId")
        text_value = element.get("text")
        text_id = element.get("id", "")
        if (
            isinstance(container_id, str)
            and isinstance(text_value, str)
            and text_value.strip()
            and isinstance(text_id, str)
        ):
            # Last writer wins if multiple text elements bind to the
            # same container — uncommon but possible after copy/paste.
            container_to_text[container_id] = text_value
            text_id_to_container[text_id] = container_id
    return _LabelIndex(container_to_text, text_id_to_container)


def _format_arrow(element: dict[str, Any], label_index: _LabelIndex) -> str:
    # _resolve_endpoint returns a fully-fenced string in all cases —
    # either a sentinel (`<label>(unbound)</label>` / `<label>(unresolved)</label>`)
    # or a candidate label wrapped in `<label>...</label>`.
    start = _resolve_endpoint(element.get("startBinding"), label_index)
    end = _resolve_endpoint(element.get("endBinding"), label_index)

    arrow_label = _arrow_label(element, label_index)
    base = f"{start} -> {end}"
    if arrow_label is not None:
        return f"{base} (labeled: <label>{arrow_label}</label>)"
    return base


def _resolve_endpoint(binding: Any, label_index: _LabelIndex) -> str:
    """Return a fully-fenced endpoint string.

    Sentinels are already full `<label>...</label>` strings. Resolved
    labels are escaped and wrapped here so the caller always gets a
    uniformly fenced value and no double-wrapping occurs.
    """
    if not isinstance(binding, dict):
        return _UNBOUND
    target_id = binding.get("elementId")
    if not isinstance(target_id, str):
        return _UNBOUND
    label = label_index.label_for(target_id)
    if label is None:
        return _UNRESOLVED
    return f"<label>{_escape(label)}</label>"


def _arrow_label(element: dict[str, Any], label_index: _LabelIndex) -> str | None:
    # 1. Inline `label.text` (older Excalidraw shape).
    inline = element.get("label")
    if isinstance(inline, dict):
        inline_text = inline.get("text")
        if isinstance(inline_text, str) and inline_text.strip():
            return _escape(inline_text)
    # 2. A text element bound via containerId — same lookup as shapes.
    arrow_id = element.get("id")
    if isinstance(arrow_id, str):
        bound = label_index.label_for(arrow_id)
        if bound is not None:
            return _escape(bound)
    return None


def _position_hint(element: dict[str, Any], type_str: str) -> str:
    x = _as_int(element.get("x"))
    y = _as_int(element.get("y"))
    width = _as_int(element.get("width"))
    height = _as_int(element.get("height"))
    return f"{type_str} at ({x},{y}) size {width}x{height}"


def _as_int(value: Any) -> int:
    try:
        return int(cast("float", value))
    except (TypeError, ValueError):
        return 0


def _escape(value: str) -> str:
    """HTML-escape `<`, `>`, `&` so labels can't break the `<label>` fence."""
    return html.escape(value, quote=False)


def _render_sections(
    components: list[str],
    connections: list[str],
    annotations: list[str],
    unnamed: list[str],
) -> str:
    """Compose the four sections in stable order with a fixed shape.

    Always emits all four headers — even with empty bodies — so the
    brain prompt sees a stable structure across turns. An empty
    section's body is the literal `(none)`.
    """

    def _section(header: str, body: list[str]) -> str:
        rendered = ", ".join(body) if body else "(none)"
        return f"{header} {rendered}"

    return "\n".join(
        [
            _section("Components:", components),
            _section("Connections:", connections),
            _section("Annotations:", annotations),
            _section("Unnamed shapes:", unnamed),
        ]
    )


def _truncate_to_byte_cap(text: str, cap_bytes: int) -> str:
    """Trim the text so its UTF-8 encoding is ≤ cap_bytes.

    Truncation happens at a code-point boundary (we encode then decode
    with `errors="ignore"` to drop a partial multi-byte sequence) and
    appends `[truncated — N more bytes elided]`. The trim leaves room
    for the marker so the final byte count stays under the cap.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    marker_template = "\n[truncated — {} more bytes elided]"
    # Reserve marker space using a generous upper bound for the digit
    # count of `elided` (the encoded length is the absolute ceiling).
    reserve = len(marker_template.format(len(encoded)).encode("utf-8"))
    head_bytes = max(cap_bytes - reserve, 0)
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    elided = len(encoded) - len(head.encode("utf-8"))
    return head + marker_template.format(elided)


__all__ = ["parse_scene"]
