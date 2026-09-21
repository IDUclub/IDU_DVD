"""Execute admin JavaScript regressions with Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["admin_jobs.test.cjs", "admin_metadata.test.cjs"])
def test_admin_javascript(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for admin JavaScript tests")
    result = subprocess.run(
        [node, f"tests/js/{script}"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
