"""Execute the admin queue's JavaScript regressions with Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_admin_queue_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for admin JavaScript tests")
    result = subprocess.run(
        [node, "tests/js/admin_jobs.test.cjs"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
