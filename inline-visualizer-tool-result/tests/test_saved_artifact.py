"""The durable object is the returned embed, not the current chat DOM or IDB."""
import asyncio
import importlib.util
import inspect
import json
import re
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("iv_saved", Path(__file__).parents[1] / "tool.py")
iv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iv)

FRAGMENT = '<div id="value"></div><script>document.getElementById("value").textContent = JSON.stringify(getToolData());</script>'


def artifact_from(html):
    match = re.search(r'<script id="iv-visualization-artifact" type="application/json">(.*?)</script>', html, re.S)
    assert match
    return json.loads(match.group(1))


def render(html=FRAGMENT, data=None, **kwargs):
    return asyncio.run(iv.Tools()._render_completed_html(
        source_tool_call_id="call:data:123", html=html,
        __messages__=[{"role": "tool", "tool_call_id": "call:data:123", "content": json.dumps(data)}],
        **kwargs,
    ))


def test_finished_html_is_a_required_tool_argument():
    parameter = inspect.signature(iv.Tools._render_completed_html).parameters["html"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


@pytest.mark.parametrize("data", [None, True, 7, "plain text", [], {"x": [1, 2, 3]}])
def test_embed_carries_complete_artifact_and_can_be_rebuilt_without_source_call(data):
    response, context = render(data=data)
    source = response.body.decode()
    artifact = artifact_from(source)
    assert artifact["schemaVersion"] == 1
    assert artifact["runtimeVersion"] == iv._IV_BUILD
    assert artifact["sourceToolCallId"] == "call:data:123"
    assert artifact["html"] == FRAGMENT
    assert artifact["data"] == data
    assert re.fullmatch(r"[a-f0-9]{32}", artifact["visualizationId"])
    assert artifact["visualizationId"] in context
    assert '"sourceMode":"saved"' in source
    assert iv.SAVED_VISUALIZATION_SCRIPT in source
    assert iv.STREAMING_OBSERVER_SCRIPT not in source
    # A serialization round trip represents storage in OWUI's saved embed.
    persisted = json.loads(json.dumps(artifact))
    rebuilt = iv._build_html(artifact=persisted)
    assert artifact_from(rebuilt) == artifact
    assert '"lifecycleKey":"' + artifact["visualizationId"] + '"' in rebuilt


def test_html_and_data_are_safely_serialized_without_loss():
    html = '<div title="&quot;">Привет &amp; [21, 11, 4]</div><script>var x = "<scr" + "ipt>";</script>'
    data = {"attack": "</script><script>window.pwned=true</script>", "unicode": "a\u2028b\u2029c"}
    response, _ = render(html, data)
    artifact = artifact_from(response.body.decode())
    assert artifact["html"] == html
    assert artifact["data"] == data
    assert b"</script><script>window.pwned" not in response.body


@pytest.mark.parametrize("html", [None, 12, "", "plain text", "```html\n<div>x</div>\n```", "@@@VIZ-START\n<div>x</div>\n@@@VIZ-END"])
def test_invalid_fragment_emits_nothing(html):
    events = []

    async def emit(event):
        events.append(event)

    result = render(html=html, __event_emitter__=emit)
    assert result["status"] == "error"
    assert result["error"] == "Invalid visualization artifact"
    assert events == []


def test_retry_preserves_exact_fragment_until_source_is_available():
    tool = iv.Tools()
    first = asyncio.run(tool._render_completed_html(source_tool_call_id="id", html=FRAGMENT))
    assert first["retry"]["arguments"]["html"] == FRAGMENT
    response, _ = asyncio.run(tool._render_completed_html(
        **first["retry"]["arguments"],
        __messages__=[{"role": "tool", "tool_call_id": "id", "content": "{\"value\":42}"}],
    ))
    assert artifact_from(response.body.decode())["data"] == {"value": 42}


def test_each_new_visualization_gets_its_own_identity():
    first = artifact_from(render()[0].body.decode())
    second = artifact_from(render()[0].body.decode())
    assert first["visualizationId"] != second["visualizationId"]


def test_new_renderer_passes_srcdoc_guard():
    iv._assert_srcdoc_safe("saved renderer", iv.SAVED_VISUALIZATION_SCRIPT)


def test_saved_mode_does_not_construct_a_source_hider():
    # Execute the real manager rather than relying on instruction wording.
    from test_tool_result import _run_lifecycle_js
    result = _run_lifecycle_js("""
const f=makeTestFrame(1);
f.closest=()=>{throw new Error('message DOM must not be consulted');};
manager.watch(f,{lifecycleVersion:5,sourceMode:'saved',lifecycleKey:'stable',maxActiveVisualizations:2});
console.log(JSON.stringify(manager.stats()));
""")
    assert result["states"] == {"streaming": 1}
