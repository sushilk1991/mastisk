"""Runs the extension's own Node tests (server choice, public links) under the pytest gate."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_extension_node_tests_pass() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the extension unit tests")

    result = subprocess.run(
        [node, "--test", "--test-timeout=20000", "extension/tests/*.test.js"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # A glob that matched nothing would also exit 0.
    assert "# fail 0" in result.stdout or "ℹ fail 0" in result.stdout, result.stdout
    assert "# pass 0" not in result.stdout and "ℹ pass 0" not in result.stdout, result.stdout
