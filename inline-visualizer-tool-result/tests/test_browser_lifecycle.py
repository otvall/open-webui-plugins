"""Opt-in real-browser regression; requires Node, Playwright and Chromium."""
import importlib.util
import asyncio
import json
import os
from pathlib import Path
import shutil
import re
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


@pytest.mark.skipif(not os.environ.get("IV_BROWSER_TESTS"), reason="Set IV_BROWSER_TESTS=1 to run Playwright")
def test_saved_embeds_restore_without_chat_dom_or_browser_storage():
    spec = importlib.util.spec_from_file_location("iv_saved_browser", Path(__file__).parents[1] / "tool.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    fragment = '''<div id="value"></div><div id="identity"></div><canvas id="chart" width="300" height="150"></canvas>
<script data-iv-libraries="chartjs">
document.getElementById('value').textContent = getToolData().value;
document.getElementById('identity').textContent = window.__ivRuntimeConfig.lifecycleKey;
new Chart(document.getElementById('chart'), {type:'bar',data:{labels:['value'],datasets:[{data:[getToolData().value]}]}});
</script>'''
    fixtures = {"valid": [], "ids": []}
    for i in range(4):
        response, _ = asyncio.run(tool.Tools()._render_completed_html(
            source_tool_call_id=f"source-{i}", html=fragment,
            __messages__=[{"role":"tool", "tool_call_id":f"source-{i}", "content":{"value":10 * (i + 1)}}],
        ))
        source = response.body.decode()
        artifact = json.loads(re.search(r'<script id="iv-visualization-artifact" type="application/json">(.*?)</script>', source, re.S)[1])
        fixtures["valid"].append(source)
        fixtures["ids"].append(artifact["visualizationId"])
    artifact = tool._build_saved_artifact(fragment, {"value":10}, "source", "Test")
    fixtures["unsupported"] = tool._build_html(artifact={**artifact, "schemaVersion":999}, chime=False)
    fixtures["missingData"] = tool._build_html(artifact={k:v for k,v in artifact.items() if k != "data"}, chime=False)
    fixtures["malformed"] = re.sub(
        r'(<script id="iv-visualization-artifact" type="application/json">).*?(</script>)',
        r'\1{invalid\2', fixtures["valid"][0], flags=re.S,
    )
    for key, html in {
        "badScript": '<div>chart</div><script>throw new Error("broken generated script");</script>',
        "plotly": '<div id="chart"></div><script data-iv-libraries="plotly">Plotly.newPlot("chart", []);</script>',
    }.items():
        fixtures[key] = tool._build_html(artifact=tool._build_saved_artifact(html, None, "source", "Test"), chime=False)
    completed = subprocess.run(
        [shutil.which("node") or "node", str(Path(__file__).with_name("browser_saved_restore.cjs"))],
        input=json.dumps(fixtures), text=True, capture_output=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"restored":True, "coldContext":True, "storageBlocked":True, "chatRequests":0, "errorsVisible":True}
