from __future__ import annotations

from typing import Any

import pytest

from scripts.deploy import DeploymentError, validate_directory_runtime_release
from turnstile_core.config import core_runtime_digest


def configured() -> dict[str, dict[str, Any]]:
    return {
        target: {
            "DIRECTORY_SOURCE": "database",
            "DIRECTORY_EXPECTED_CORE_DIGEST": core_runtime_digest(),
        }
        for target in ("api", "telemetry", "control-plane")
    }


def test_matching_directory_trio_and_legacy_release_are_allowed() -> None:
    validate_directory_runtime_release(configured())
    validate_directory_runtime_release({"api": {"DIRECTORY_SOURCE": "legacy"}})


@pytest.mark.parametrize("change", ["old-digest", "mixed-source", "missing-target"])
def test_directory_release_rejects_incompatible_core_before_upload(change: str) -> None:
    settings = configured()
    if change == "old-digest":
        settings["api"]["DIRECTORY_EXPECTED_CORE_DIGEST"] = "0" * 64
    elif change == "mixed-source":
        settings["telemetry"]["DIRECTORY_SOURCE"] = "legacy"
    else:
        del settings["control-plane"]
    with pytest.raises(DeploymentError, match="maintenance update"):
        validate_directory_runtime_release(settings)
