from urllib.parse import quote

import pytest

from backend.services.download_filename import attachment_header, plugin_filename


@pytest.mark.parametrize(("name", "expected"), [
    ("Edwin", "Edwin"),
    ("Edwin-Agent", "Edwin-Agent"),
    (" Cc Switch ", "Cc-Switch"),
    ("研发团队", "研发团队"),
    ('../../研发 "Agent": A\\B\r\n', "研发-Agent-A-B"),
    ("René", "René"),
    ("a\u202eb", "a-b"),
    ("..", "user"),
    ("", "user"),
    ("03ba5bbc-07df-42bb-a13d-6faebf421ae2", "user"),
    ("03ba5bbc07df42bba13d6faebf421ae2", "user"),
])
def test_display_name_filename(name: str, expected: str) -> None:
    filename = plugin_filename("aigateway-proxy-user", name, "user")
    assert filename == f"aigateway-proxy-user-{expected}.zip"
    header = attachment_header(filename)
    assert header.isascii()
    assert f"filename*=UTF-8''{quote(filename)}" in header
    assert "\r" not in header and "\n" not in header


def test_long_names_fit_portable_filename_limits() -> None:
    filename = plugin_filename("aigateway-proxy-user", "用户" * 100, "user")
    assert len(filename.encode("utf-8")) <= len("aigateway-proxy-user-.zip") + 80
    assert filename.endswith(".zip")


def test_unicode_only_name_has_meaningful_ascii_fallback() -> None:
    header = attachment_header(plugin_filename("aigateway-proxy", "研发", "subscription"))
    assert 'filename="aigateway-proxy-download.zip"' in header
