"""Every page the sidebar offers has to survive the data source's page normaliser.

`normalizeApimPage` sends anything it does not recognise to `finops-overview`. A page added
to the sidebar but not to `apimPageIds` therefore renders a nav row that silently returns the
operator to the overview -- no error, no blank screen, nothing in the console, just a link
that appears to do nothing. That shipped once: the organization page was wired into the nav,
the page union, `pageIds` and the render switch, and was unreachable because the fifth
registry was missed.

The two lists are in different files by design -- one describes the shell, the other belongs
to the data source that owns those pages -- so keeping them in step needs a check rather than
a convention.
"""

from __future__ import annotations

import re

from tests.support.paths import FRONTEND_SOURCE

APP = FRONTEND_SOURCE / "app.tsx"
APIM_SOURCE = FRONTEND_SOURCE / "data-sources/apim/source.tsx"


def _sidebar_pages() -> set[str]:
    return set(re.findall(r'page:\s*"([a-z0-9-]+)"', APP.read_text(encoding="utf-8")))


def _normalised_pages() -> set[str]:
    source = APIM_SOURCE.read_text(encoding="utf-8")
    listed = re.search(r"const apimPageIds = new Set\(\[(.*?)\]\)", source, flags=re.S)
    assert listed is not None, "apimPageIds is no longer a literal set; update this check"
    pages = re.search(r"const apimPages = \[(.*?)\] as const", source, flags=re.S)
    assert pages is not None, "apimPages is no longer a literal array; update this check"
    return set(re.findall(r'"([a-z0-9-]+)"', listed.group(1))) | set(
        re.findall(r'id:\s*"([a-z0-9-]+)"', pages.group(1))
    )


def test_every_sidebar_page_survives_normalisation() -> None:
    sidebar = _sidebar_pages()
    # A guard against a refactor that empties the parse and makes this vacuously pass.
    assert len(sidebar) > 5
    assert sidebar <= _normalised_pages()


def test_the_organization_page_is_reachable() -> None:
    assert "organization" in _sidebar_pages()
    assert "organization" in _normalised_pages()
    assert '=== "organization" && <OrganizationPage />' in APP.read_text(encoding="utf-8")
