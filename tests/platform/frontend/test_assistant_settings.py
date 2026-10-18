from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT


def test_assistant_settings_draft_rules() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("assistant-settings.test.mjs"))],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
