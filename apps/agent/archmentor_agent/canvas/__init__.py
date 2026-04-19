"""Excalidraw scene parser + diff.

Converts scene JSON to a compact text description for the brain prompt.
Includes components, connections (labeled arrows), annotations,
unnamed shapes, and spatial grouping. Resolves arrow bindings via
`startBinding.elementId`.

Implementation lands in M3.
"""
