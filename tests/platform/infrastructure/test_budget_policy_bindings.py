from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from scripts.budget_policy_bindings import bind_policy_parameters


def test_binding_covers_attributes_text_and_tail_without_mutating_the_template() -> None:
    template = ET.fromstring(
        '<policy name="__NAME__">__BODY__<child />__TAIL__</policy>',
    )
    original = ET.tostring(template)
    bound = bind_policy_parameters(template, {
        "__NAME__": 'existing-logger&"', "__BODY__": "body < value", "__TAIL__": "tail & value",
    })
    assert bound.get("name") == 'existing-logger&"'
    assert bound.text == "body < value"
    assert bound[0].tail == "tail & value"
    assert ET.tostring(template) == original
    assert ET.tostring(ET.fromstring(ET.tostring(bound))) == ET.tostring(bound)


def test_binding_allows_unused_values_and_explicit_empty_slots() -> None:
    template = ET.fromstring('<policy optional="__OPTIONAL__" />')
    bound = bind_policy_parameters(template, {"__OPTIONAL__": "", "__UNUSED__": "unused"})
    assert bound.get("optional") == ""


@pytest.mark.parametrize("source", [
    '<policy name="__MISSING__" />',
    "<policy>__MISSING__</policy>",
    "<policy><child />__MISSING__</policy>",
])
def test_binding_refuses_missing_parameters_without_mutating_the_template(source: str) -> None:
    template = ET.fromstring(source)
    original = ET.tostring(template)
    with pytest.raises(ValueError, match="missing bindings.*__MISSING__"):
        bind_policy_parameters(template, {})
    assert ET.tostring(template) == original


@pytest.mark.parametrize("replacement", ["__OTHER__", "prefix __OTHER__ suffix", "__VALUE__"])
def test_binding_never_recursively_resolves_parameter_values(replacement: str) -> None:
    template = ET.fromstring('<policy name="__VALUE__" />')
    with pytest.raises(ValueError, match="unresolved parameters"):
        bind_policy_parameters(template, {"__VALUE__": replacement, "__OTHER__": "nested"})
