"""Opt-in real-browser regression; requires Node, Playwright and Chromium."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not os.environ.get("IV_BROWSER_TESTS"), reason="Set IV_BROWSER_TESTS=1 to run Playwright")
def test_many_iframes_admit_before_libraries_and_restore_from_indexeddb():
    spec = importlib.util.spec_from_file_location("iv_browser_tool", Path(__file__).parents[1] / "tool.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    fixtures = [tool._build_html(chime=False, lifecycle_key=f"chart-{i}", message_id=f"m{i}") for i in range(20)]
    completed = subprocess.run(
        [shutil.which("node") or "node", str(Path(__file__).with_name("browser_lifecycle.cjs"))],
        input=json.dumps(fixtures), text=True, capture_output=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["peakHeavy"] == 2
    assert result["rendered"] == 3
