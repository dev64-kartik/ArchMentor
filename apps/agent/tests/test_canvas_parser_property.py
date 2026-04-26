"""Property-based coverage for `parse_scene`.

The hand-written tests cover representative scenes; the property test
asserts invariants across the random tail: parser doesn't raise on
arbitrary scene shapes, output stays inside the byte cap, and label
fences balance regardless of label content. Triggered by R17 / Q11 in
the M3 plan.
"""

from __future__ import annotations

from typing import Any

from archmentor_agent.canvas import parse_scene
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Keep example counts modest — the parser is fast but the harness
# spawns Hypothesis state for every test method.
_SETTINGS = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_OUTPUT_CAP_BYTES = 8 * 1024


# --- strategies ---------------------------------------------------------

# Reuses Excalidraw's element vocabulary; including known + unknown
# types so the parser proves it handles the long tail of v1 + v2 shapes.
_known_types = st.sampled_from(
    [
        "rectangle",
        "ellipse",
        "diamond",
        "arrow",
        "line",
        "text",
        "image",
        "frame",
        "freedraw",
    ]
)
_unknown_types = st.text(
    min_size=1, max_size=20, alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E)
)
_element_type = st.one_of(_known_types, _unknown_types)


def _element_strategy(known_ids: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    coord = st.integers(min_value=-10_000, max_value=10_000)
    size = st.integers(min_value=0, max_value=5_000)
    label_text = st.text(
        min_size=0,
        max_size=120,
        alphabet=st.characters(
            blacklist_categories=("Cs",),  # surrogates: invalid in Python strings
        ),
    )
    binding = st.fixed_dictionaries(
        {"elementId": st.sampled_from(known_ids) if known_ids else st.just("ghost")}
    )

    @st.composite
    def _one(draw: st.DrawFn) -> dict[str, Any]:
        eid = draw(st.text(min_size=1, max_size=10, alphabet="abcdef0123456789"))
        etype = draw(_element_type)
        element: dict[str, Any] = {
            "id": eid,
            "type": etype,
            "x": draw(coord),
            "y": draw(coord),
            "width": draw(size),
            "height": draw(size),
        }
        # Text elements carry a `text` field and may bind to a container.
        if etype == "text":
            element["text"] = draw(label_text)
            if known_ids and draw(st.booleans()):
                element["containerId"] = draw(st.sampled_from(known_ids))
        # Arrow / line elements may bind to known or unknown ids.
        if etype in {"arrow", "line"}:
            if draw(st.booleans()):
                element["startBinding"] = draw(binding)
            if draw(st.booleans()):
                element["endBinding"] = draw(binding)
            if draw(st.booleans()):
                element["label"] = {"text": draw(label_text)}
        return element

    return _one()


@st.composite
def excalidraw_scene(draw: st.DrawFn) -> dict[str, Any]:
    # Two-pass to give text/arrows realistic ids to bind against.
    raw_ids = draw(
        st.lists(
            st.text(min_size=1, max_size=10, alphabet="abcdef0123456789"),
            min_size=0,
            max_size=15,
            unique=True,
        )
    )
    # Build elements with ids drawn from `raw_ids` so bindings hit real
    # elements at non-trivial frequency.
    elements: list[dict[str, Any]] = []
    for fixed_id in raw_ids:
        element = draw(_element_strategy(raw_ids))
        element["id"] = fixed_id
        elements.append(element)
    return {"elements": elements, "appState": {}}


# --- properties ---------------------------------------------------------


@_SETTINGS
@given(scene=excalidraw_scene())
def test_parser_never_raises(scene: dict[str, Any]) -> None:
    parse_scene(scene)


@_SETTINGS
@given(scene=excalidraw_scene())
def test_output_within_byte_cap(scene: dict[str, Any]) -> None:
    out = parse_scene(scene)
    assert len(out.encode("utf-8")) <= _OUTPUT_CAP_BYTES


@_SETTINGS
@given(scene=excalidraw_scene())
def test_label_tags_balance(scene: dict[str, Any]) -> None:
    """Adversarial label content must never break the fence."""
    out = parse_scene(scene)
    open_count = out.count("<label>")
    close_count = out.count("</label>")
    assert open_count == close_count


@_SETTINGS
@given(scene=excalidraw_scene())
def test_all_four_section_headers_present(scene: dict[str, Any]) -> None:
    """Stable shape matters more than economy — the brain prompt assumes
    four headers exist on every turn."""
    out = parse_scene(scene)
    for header in ("Components:", "Connections:", "Annotations:", "Unnamed shapes:"):
        assert header in out
