"""Strict XML parameter binding shared by offline budget installation tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from xml.etree import ElementTree as ET

_SLOT = re.compile(r"__[A-Z][A-Z0-9_]*__")


def bind_policy_parameters(template: ET.Element, bindings: Mapping[str, str]) -> ET.Element:
    """Bind a copy once; neither missing nor recursively introduced slots are allowed."""
    def bind(value: str) -> str:
        missing = set(_SLOT.findall(value)) - bindings.keys()
        if missing:
            raise ValueError("Budget policy is missing bindings: " + ", ".join(sorted(missing)))
        result = _SLOT.sub(lambda match: bindings[match.group()], value)
        if _SLOT.search(result):
            raise ValueError("Budget policy binding contains unresolved parameters")
        return result

    root = deepcopy(template)
    for node in root.iter():
        for key, value in node.attrib.items():
            node.set(key, bind(value))
        if node.text:
            node.text = bind(node.text)
        if node.tail:
            node.tail = bind(node.tail)
    return root
