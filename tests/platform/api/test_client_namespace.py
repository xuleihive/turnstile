import re

import pytest

from backend.services.client_namespace import plugin_namespace

UID = "03ba5bbc-07df-42bb-a13d-6faebf421ae2"


@pytest.mark.parametrize(("name", "label"), [
    ("Edwin", "edwin"),
    ("Edwin-Agent", "edwin-agent"),
    ("研发团队", "xn--oor44emy8bbez"),
    ('../../研发 "Agent": A\\B\r\n', "xn---agent-a-b-os5q9495a"),
    ("Ｒｅｎé", "xn--ren-dma"),
    ("a\u202eb", "a-b"),
    ("", "user"),
    (UID, "user"),
    (UID.replace("-", ""), "user"),
])
def test_namespace_uses_safe_display_name_not_raw_identity(name: str, label: str) -> None:
    result = plugin_namespace("user", name, "https://platform.example", UID)
    assert result.startswith(f"aigateway-user-{label}-")
    assert re.fullmatch(r"[a-f0-9]{12}", result.rsplit("-", 1)[-1])
    assert UID not in result and UID.replace("-", "") not in result
    assert "/" not in result and "\\" not in result and ".." not in result
    assert result.isascii()


def test_same_names_platforms_and_plugin_modes_do_not_share_namespaces() -> None:
    base = plugin_namespace("user", "Edwin", "https://one.example", UID)
    assert plugin_namespace("user", "Edwin", "https://one.example", UID) == base
    assert plugin_namespace("user", "Edwin", "https://two.example", UID) != base
    assert plugin_namespace("user", "Edwin", "https://one.example", "another-user") != base
    assert plugin_namespace("subscription", "Edwin", "https://one.example", UID) != base


def test_unicode_label_has_bounded_bytes_and_no_partial_character() -> None:
    result = plugin_namespace("subscription", "用户" * 100, "gateway", UID)
    assert len(result.encode()) < 160
    assert "\ufffd" not in result
