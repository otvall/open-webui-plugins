import asyncio
import importlib.util
import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

MODULE_PATH = Path(__file__).parents[1] / "tool.py"
SPEC = importlib.util.spec_from_file_location(
    "inline_visualizer_tool_result", MODULE_PATH
)
iv = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(iv)


class DummyRequest:
    def __init__(self):
        self.state = SimpleNamespace()
        self.app = SimpleNamespace(state=SimpleNamespace())


def run(awaitable):
    return asyncio.run(awaitable)


def response_output(*calls):
    output = []
    for call_id, result in calls:
        output.append(
            {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": "execute_sql",
                "arguments": json.dumps({"sql": f"secret-{call_id}"}),
                "status": "completed",
            }
        )
        output.append(
            {
                "type": "function_call_output",
                "id": f"output-{call_id}",
                "call_id": call_id,
                "output": [
                    {
                        "type": "input_text",
                        "text": (
                            result
                            if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False)
                        ),
                    }
                ],
                "status": "completed",
                "files": [{"name": f"private-{call_id}.csv"}],
            }
        )
    return output


def tool_message(call_id, result):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        ),
    }


def extract_tool_json(html):
    match = re.search(
        r'<script id="iv-tool-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        match = re.search(r'<script id="iv-visualization-artifact" type="application/json">(.*?)</script>', html, re.DOTALL)
        assert match is not None
        return json.loads(match.group(1))["data"]
    return json.loads(match.group(1))


def capture_visualize(tool, **kwargs):
    kwargs.setdefault("html", "<div>Saved chart</div>")
    events = []

    async def emitter(event):
        events.append(event)

    returned = run(tool._render_completed_html(__event_emitter__=emitter, **kwargs))
    result = returned[1] if isinstance(returned, tuple) else returned
    html = events[0]["data"]["embeds"][0] if events else None
    return result, html, events


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ([{"x": 1}], [{"x": 1}]),
        ({"x": 1}, {"x": 1}),
        ('[{"x":1}]', [{"x": 1}]),
        ('{"x":1}', {"x": 1}),
        ('"value"', "value"),
        ("null", None),
        ("plain text", "plain text"),
        (None, None),
    ],
)
def test_normalize_tool_result(source, expected):
    assert iv._normalize_tool_result(source) == expected


def test_extract_text_content_omits_images_and_files():
    content = [
        {"type": "input_text", "text": '{"value":1}'},
        {"type": "input_image", "image_url": "data:image/png;base64,secret"},
        {"type": "file", "url": "/private.csv"},
    ]

    assert iv._extract_text_content(content) == '{"value":1}'
    assert iv._extract_text_content(
        [{"type": "input_image", "image_url": "data:image/png;base64,secret"}]
    ) == ""


def test_find_output_result_matches_exact_id_and_uses_latest_duplicate():
    output = response_output(
        ("call-same", [{"version": 1}]),
        ("call-other", [{"other": True}]),
        ("call-same", [{"version": 2}]),
    )

    found, result = iv._find_output_tool_result(output, "call-same")

    assert found is True
    assert result == [{"version": 2}]


def test_find_output_result_requires_completed_result_item():
    output = [
        {
            "type": "function_call",
            "call_id": "call-pending",
            "name": "execute_sql",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "call_id": "call-pending",
            "output": [{"type": "input_text", "text": '{"partial":true}'}],
            "status": "in_progress",
        },
    ]

    assert iv._find_output_tool_result(output, "call-pending") == (False, None)


def test_find_message_result_matches_exact_id_and_uses_latest_duplicate():
    messages = [
        tool_message("call-same", [{"version": 1}]),
        tool_message("call-other", [{"other": True}]),
        tool_message("call-same", [{"version": 2}]),
    ]

    found, result = iv._find_message_tool_result(messages, "call-same")

    assert found is True
    assert result == [{"version": 2}]


def test_find_message_result_accepts_saved_response_output_from_dialogue():
    messages = [
        {
            "role": "assistant",
            "output": response_output(
                ("function.execute_sql:0", [{"source": "earlier"}])
            ),
        },
        {
            "role": "assistant",
            "output": response_output(
                ("function.execute_sql:0", [{"source": "latest"}])
            ),
        },
    ]

    found, result = iv._find_message_tool_result(
        messages, "function.execute_sql:0"
    )

    assert found is True
    assert result == [{"source": "latest"}]


def test_find_message_result_accepts_direct_responses_api_item():
    messages = [
        {
            "type": "function_call_output",
            "call_id": "call_provider_generated",
            "output": [{"type": "output_text", "text": '{"ok":true}'}],
            "status": "completed",
        }
    ]

    found, result = iv._find_message_tool_result(
        messages, "call_provider_generated"
    )

    assert found is True
    assert result == {"ok": True}


def test_load_outputs_returns_active_stream_before_stored_output(monkeypatch):
    active_output = response_output(("call-data", [{"source": "active"}]))
    stored_output = response_output(("call-data", [{"source": "stored"}]))
    calls = []

    class FakeChats:
        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            calls.append(("stored", chat_id, message_id))
            return {"output": stored_output}

    async def get_response_streams_by_chat_id(redis, chat_id):
        calls.append(("active", redis, chat_id))
        return [
            {
                "chat_id": chat_id,
                "message_id": "assistant-message",
                "output": active_output,
            }
        ]

    open_webui_module = ModuleType("open_webui")
    open_webui_module.__path__ = []
    models_module = ModuleType("open_webui.models")
    models_module.__path__ = []
    chats_module = ModuleType("open_webui.models.chats")
    chats_module.Chats = FakeChats
    tasks_module = ModuleType("open_webui.tasks")
    tasks_module.get_response_streams_by_chat_id = get_response_streams_by_chat_id
    monkeypatch.setitem(sys.modules, "open_webui", open_webui_module)
    monkeypatch.setitem(sys.modules, "open_webui.models", models_module)
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", chats_module)
    monkeypatch.setitem(sys.modules, "open_webui.tasks", tasks_module)

    request = DummyRequest()
    request.app.state.redis = "redis-connection"
    candidates = run(
        iv._load_current_message_outputs(
            request,
            {"chat_id": "chat", "message_id": "assistant-message"},
        )
    )

    assert candidates == [active_output, stored_output]
    assert calls == [
        ("active", "redis-connection", "chat"),
        ("stored", "chat", "assistant-message"),
    ]


def test_resolve_result_prefers_active_stream_over_stored_output(monkeypatch):
    async def load_outputs(request, metadata):
        return [
            response_output(("call-data", [{"source": "active"}])),
            response_output(("call-data", [{"source": "stored"}])),
        ]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)

    found, result = run(
        iv._resolve_tool_result(
            "call-data",
            DummyRequest(),
            {"chat_id": "chat", "message_id": "assistant-message"},
            [tool_message("call-data", [{"source": "messages"}])],
        )
    )

    assert found is True
    assert result == [{"source": "active"}]


def test_resolve_result_falls_back_to_messages(monkeypatch):
    async def load_outputs(request, metadata):
        return [response_output(("call-other", [{"other": True}]))]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)

    found, result = run(
        iv._resolve_tool_result(
            "call-data",
            DummyRequest(),
            {},
            [tool_message("call-data", [{"source": "messages"}])],
        )
    )

    assert found is True
    assert result == [{"source": "messages"}]


def test_source_tool_call_id_is_required_in_public_signature():
    parameter = inspect.signature(iv.Tools._render_completed_html).parameters[
        "source_tool_call_id"
    ]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


def test_retry_attempt_is_bounded_in_public_signature():
    parameter = inspect.signature(iv.Tools._render_completed_html).parameters[
        "retry_attempt"
    ]

    assert parameter.default == 0
    assert parameter.annotation == iv.Literal[0, 1]


def test_visualize_without_event_emitter_returns_html_response_tuple():
    response, context = run(
        iv.Tools()._render_completed_html(
            source_tool_call_id="call-data",
            html="<div>Saved chart</div>",
            title="Fallback",
            __messages__=[tool_message("call-data", [{"x": 1}])],
            __metadata__={"chat_id": "chat/live", "message_id": "msg:live"},
        )
    )

    assert isinstance(response, iv.HTMLResponse)
    assert response.headers["content-disposition"] == "inline"
    assert "self-contained embed" in context
    assert b"getToolData" in response.body
    assert b'tool-result-1.4.3' in response.body
    runtime = re.search(
        rb'<script id="iv-runtime-config" type="application/json">(.*?)</script>',
        response.body,
    )
    assert runtime is not None
    config = json.loads(runtime.group(1))
    assert re.fullmatch(r"[0-9a-f]{32}", config["lifecycleKey"])
    assert config["chatId"] == "chat/live"
    assert config["messageId"] == "msg:live"


def test_event_emitter_persists_message_embed_and_html_response_is_still_returned():
    events = []

    async def emitter(event):
        events.append(event)

    response, context = run(
        iv.Tools()._render_completed_html(
            source_tool_call_id="call-data",
            html="<div>Saved chart</div>",
            title="Dual path",
            __messages__=[tool_message("call-data", [{"x": 1}])],
            __event_emitter__=emitter,
        )
    )

    assert isinstance(response, iv.HTMLResponse)
    assert "self-contained embed" in context
    assert events == [
        {
            "type": "embeds",
            "data": {
                "embeds": [response.body.decode("utf-8")],
                "replace": False,
            },
        }
    ]


def test_visualize_injects_only_the_selected_result():
    messages = [
        tool_message("call-a", [{"marker": "DO_NOT_INCLUDE_A"}]),
        {
            "role": "assistant",
            "content": "metadata-secret",
            "tool_calls": [
                {
                    "id": "call-b",
                    "function": {
                        "name": "execute_sql",
                        "arguments": '{"sql":"SELECT secret"}',
                    },
                }
            ],
        },
        tool_message("call-b", [{"marker": "ONLY_SELECTED_B"}]),
        tool_message("call-c", [{"marker": "DO_NOT_INCLUDE_C"}]),
    ]

    result, html, _ = capture_visualize(
        iv.Tools(),
        title="Selected",
        source_tool_call_id="call-b",
        __messages__=messages,
        __metadata__={"private": "metadata-secret-value"},
    )

    assert "getToolData()" in result
    assert extract_tool_json(html) == [{"marker": "ONLY_SELECTED_B"}]
    assert "DO_NOT_INCLUDE_A" not in html
    assert "DO_NOT_INCLUDE_C" not in html
    assert "SELECT secret" not in html
    assert "metadata-secret" not in html
    assert "getToolData" in html


@pytest.mark.parametrize(
    "payload",
    [
        [{"kind": "list"}],
        {"kind": "dict"},
        "plain string",
        "Русский текст",
        42,
        True,
        None,
    ],
)
def test_tool_data_bridge_round_trip_for_supported_shapes(payload):
    bridge = iv._build_tool_data_bridge(payload)
    assert extract_tool_json(bridge) == payload


def test_safe_json_blocks_script_breakout_and_preserves_unicode():
    payload = {
        "ru": "Привет",
        "attack": "</script><script>window.pwned=true</script>",
        "symbols": "<>&",
        "separators": "before\u2028middle\u2029after",
    }
    bridge = iv._build_tool_data_bridge(payload)

    assert "</script><script>window.pwned" not in bridge
    assert "\\u003c/script\\u003e" in bridge
    assert "\\u0026" in bridge
    assert "\u2028" not in bridge
    assert "\u2029" not in bridge
    assert extract_tool_json(bridge) == payload


@pytest.mark.parametrize("source_tool_call_id", ["", "   "])
def test_empty_source_tool_call_id_is_a_controlled_error(source_tool_call_id):
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id=source_tool_call_id,
        __messages__=[],
    )

    assert result["status"] == "error"
    assert result["error"] == "Invalid source_tool_call_id"
    assert result["source_tool_call_id"] == source_tool_call_id
    assert html is None
    assert events == []


def test_unknown_or_unfinished_source_tool_call_id_requests_one_retry(
    monkeypatch,
):
    async def load_outputs(request, metadata):
        return [
            [
                {
                    "type": "function_call",
                    "call_id": "call-pending",
                    "name": "execute_sql",
                    "status": "completed",
                }
            ]
        ]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)

    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id="call-pending",
        __messages__=[],
    )

    assert result["status"] == "retry_required"
    assert result["code"] == "source_result_not_visible_yet"
    assert result["source_tool_call_id"] == "call-pending"
    assert result["retry"] == {
        "tool": "visualize_tool_result",
        "arguments": {
            "source_tool_call_id": "call-pending",
            "title": "Tool Result Visualization",
            "html": "<div>Saved chart</div>",
            "retry_attempt": 1,
        },
    }
    assert "do not run the data-producing tool again" in result["message"]
    assert html is None
    assert events == []


def test_second_missing_attempt_is_a_controlled_error(monkeypatch):
    async def load_outputs(request, metadata):
        return []

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)

    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id="call-still-missing",
        title="Retry",
        retry_attempt=1,
        __messages__=[],
    )

    assert result["status"] == "error"
    assert result["error"] == "Tool result not found"
    assert result["source_tool_call_id"] == "call-still-missing"
    assert "single visualization retry" in result["message"]
    assert "do not rerun the data-producing tool" in result["message"]
    assert html is None
    assert events == []


def test_retry_succeeds_when_source_result_becomes_visible():
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id="call-data",
        title="Retry success",
        retry_attempt=1,
        __messages__=[tool_message("call-data", [{"value": 42}])],
    )

    assert "self-contained embed" in result
    assert extract_tool_json(html) == [{"value": 42}]
    assert len(events) == 1


def test_invalid_retry_attempt_is_a_controlled_error():
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id="call-data",
        retry_attempt=2,
        __messages__=[tool_message("call-data", [{"value": 42}])],
    )

    assert result["status"] == "error"
    assert result["error"] == "Invalid retry_attempt"
    assert html is None
    assert events == []


def test_unserializable_result_is_a_controlled_error(monkeypatch):
    async def resolve(tool_call_id, request, metadata, messages):
        return True, float("nan")

    monkeypatch.setattr(iv, "_resolve_tool_result", resolve)

    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_call_id="call-nan",
    )

    assert result["status"] == "error"
    assert result["error"] == "Invalid visualization artifact"
    assert result["source_tool_call_id"] == "call-nan"
    assert html is None
    assert events == []


def test_tool_data_bridge_preserves_original_runtime_and_csp_order():
    bridge = iv._build_tool_data_bridge([{"x": 1}])
    html = iv._build_html(
        security_level="strict",
        tool_data_bridge=bridge,
    )

    assert html.index("function sendPrompt") < html.index('id="iv-tool-data"')
    assert html.index("getToolData") < html.index("var START_MARK")
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in html
    assert "cdnjs.cloudflare.com" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html
    assert "/static/html2canvs.js" in html
    assert "'unsafe-eval'" in html
    assert "if (src)" in html
    assert "_ivIsBlockedScript" not in html
    assert "External scripts are not allowed" not in html


def test_offline_preserves_original_self_hosted_script_policy():
    html = iv._build_html(security_level="offline")
    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self'" in html


def _run_downsampling_js(assertion_source, point_density=1):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executable browser-helper tests")
    match = re.fullmatch(
        r"\s*<script>\s*(.*?)\s*</script>\s*",
        iv.DOWNSAMPLING_SCRIPT,
        re.DOTALL,
    )
    assert match is not None
    source = (
        f"global.window={{__ivRuntimeConfig:{{pointDensity:{point_density}}}}};"
        "global.document={documentElement:{clientWidth:200},"
        "querySelector:function(){return null;}};"
        + match.group(1)
        + assertion_source
    )
    completed = subprocess.run(
        [node],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_lifecycle_js(assertion_source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executable lifecycle tests")
    match = re.fullmatch(
        r"\s*<script>\s*(.*?)\s*</script>\s*",
        iv.LIFECYCLE_BOOTSTRAP_SCRIPT,
        re.DOTALL,
    )
    assert match is not None
    source = f"""
const observers = [];
const parentHandlers = {{}};
class FakeMutationObserver {{ constructor(callback) {{ this.callback = callback; observers.push(this); }} observe() {{}} disconnect() {{this.disconnected=true;}} }}
function raf(callback) {{ callback(); return 1; }}
const parentWindow = {{addEventListener:function(name,callback){{parentHandlers[name]=callback;}}}};
const parentDocument = {{
  body: {{}},
  createElement: function() {{ return {{textContent:'', remove:function(){{}}}}; }}
}};
parentDocument.head = {{appendChild:function(element) {{
  new Function('window','document','MutationObserver','requestAnimationFrame',element.textContent)(
    parentWindow, parentDocument, FakeMutationObserver, raf
  );
}}}};
parentWindow.document = parentDocument;
const childWindow = {{frameElement:null,__ivRuntimeConfig:{{}}}};
new Function('window','parent','document','MutationObserver','requestAnimationFrame',
  {json.dumps(match.group(1))}
)(childWindow,parentWindow,{{getElementById:()=>null}},FakeMutationObserver,raf);
const manager = parentWindow.__ivLifecycleV5;
function makeTestFrame(order, source='source-'+order, snapshot=null) {{
  const attrs = {{srcdoc:source}};
  return {{order, isConnected:true, style:{{}}, parentElement:null,
    contentDocument:{{title:'Chart'}},
    contentWindow:{{_ivCreateSnapshot:()=>Promise.resolve(snapshot)}},
    getAttribute:name=>attrs[name] || '',
    setAttribute:(name,value)=>{{attrs[name]=String(value);}},
    getBoundingClientRect:()=>({{width:600,height:200}}),
    getClientRects:()=>[{{}}], closest:()=>null,
    compareDocumentPosition:other=>order<other.order?4:2
  }};
}}
{assertion_source}
"""
    completed = subprocess.run(
        [node],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_runtime_config_and_valve_defaults_are_injected():
    valves = iv.Tools.Valves()
    assert valves.max_active_visualizations == 2
    assert valves.point_density == 1.0
    assert valves.chartjs_url == "/static/chart.umd.min.js"
    assert valves.plotly_url == "/static/plotly.umd.min.js"

    html = iv._build_html(
        max_active_visualizations=4,
        point_density=1.5,
        chartjs_url="/static/custom/chart.js",
        plotly_url="/static/custom/plotly.js",
    )
    config_match = re.search(
        r'<script id="iv-runtime-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert config_match is not None
    assert json.loads(config_match.group(1)) == {
        "build": "tool-result-1.4.3",
        "lifecycleVersion": 5,
        "maxActiveVisualizations": 4,
        "pointDensity": 1.5,
        "chartjsUrl": "/static/custom/chart.js",
        "plotlyUrl": "/static/custom/plotly.js",
    }


def test_tool_forwards_startup_library_valves_and_instructs_model_not_to_import():
    tool = iv.Tools()
    tool.valves.chartjs_url = "/assets/chart.js"
    tool.valves.plotly_url = "/assets/plotly.js"
    response, context = run(
        tool._render_completed_html(
            source_tool_call_id="call-data",
            html="<div>Saved chart</div>",
            __messages__=[tool_message("call-data", [{"x": 1}])],
        )
    )
    match = re.search(
        rb'<script id="iv-runtime-config" type="application/json">(.*?)</script>',
        response.body,
    )
    assert match is not None
    config = json.loads(match.group(1))
    assert config["chartjsUrl"] == "/assets/chart.js"
    assert config["plotlyUrl"] == "/assets/plotly.js"
    assert "Do not add script tags or other loaders for Chart.js or Plotly" in context


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_active_visualizations", -1),
        ("max_active_visualizations", 11),
        ("point_density", -0.1),
        ("point_density", 4.1),
    ],
)
def test_optimization_valves_are_bounded(field, value):
    with pytest.raises(ValidationError):
        iv.Tools.Valves(**{field: value})


def test_line_downsampling_obeys_css_pixel_budget_and_keeps_peak():
    result = _run_downsampling_js(
        """
const points=Array.from({length:2000},(_,i)=>[i,i===1000?10000:Math.sin(i)]);
const before=JSON.stringify(points);
const output=window.ivDownsample(points,{container:200,x:0,y:1,mode:'line'});
console.log(JSON.stringify({
  budget:window.ivPointBudget(200,1), length:output.length,
  first:output[0][0], last:output[output.length-1][0],
  peak:output.some((point)=>point[1]===10000), unchanged:before===JSON.stringify(points)
}));
"""
    )
    assert result == {
        "budget": 200,
        "length": 200,
        "first": 0,
        "last": 1999,
        "peak": True,
        "unchanged": True,
    }


def test_scatter_downsampling_and_multi_series_share_budget():
    result = _run_downsampling_js(
        """
const points=Array.from({length:2000},(_,i)=>({x:i%100,y:Math.floor(i/100)}));
const output=window.ivDownsample(points,{
  container:200,x:'x',y:'y',mode:'scatter',seriesCount:2
});
console.log(JSON.stringify({
  budget:window.ivPointBudget(200,2), length:output.length
}));
"""
    )
    assert result["budget"] == 100
    assert result["length"] <= result["budget"]


def test_invalid_coordinates_use_endpoint_preserving_fallback():
    result = _run_downsampling_js(
        """
const points=Array.from({length:2000},(_,i)=>({label:'p'+i,value:i}));
const output=window.ivDownsample(points,{container:200,x:'missing',y:'value'});
console.log(JSON.stringify({
  length:output.length, first:output[0].label, last:output[output.length-1].label
}));
"""
    )
    assert result == {"length": 200, "first": "p0", "last": "p1999"}


def test_zero_density_disables_downsampling():
    result = _run_downsampling_js(
        """
const points=Array.from({length:2000},(_,i)=>[i,i]);
const output=window.ivDownsample(points,{container:200,x:0,y:1});
console.log(JSON.stringify({length:output.length}));
""",
        point_density=0,
    )
    assert result["length"] == 2000


def test_lifecycle_static_shell_excludes_live_payloads_and_libraries():
    source = iv.LIFECYCLE_BOOTSTRAP_SCRIPT
    assert "data-iv-static" in source
    assert "Restore interactivity" in source
    assert "iv-tool-data" not in source
    assert "getToolData" not in source
    assert "cdnjs.cloudflare.com" not in source
    assert "Chart.js" not in source


def test_lifecycle_parent_observer_cannot_loop_on_streaming_style_writes():
    source = iv.LIFECYCLE_BOOTSTRAP_SCRIPT
    assert "attributes: true" not in source
    assert "attributeFilter" not in source
    assert "window.__ivLifecycleV5" in source


def test_lifecycle_reload_keeps_latest_dom_frames_and_waits_before_snapshot():
    result = _run_lifecycle_js(
        """
const events = [];
function frame(order) {
  const attrs = {srcdoc:'original-' + order};
  return {
    order:order,isConnected:true,style:{},parentElement:null,
    contentDocument:{title:'Chart ' + order},
    contentWindow:{
      __ivSnapshotReady:function(){events.push('ready-' + order);return Promise.resolve();},
      _ivCreateSnapshot:function(){events.push('snapshot-' + order);return Promise.resolve({url:'data:image/png;base64,eA=='});}
    },
    getAttribute:function(name){return attrs[name] || '';},
    setAttribute:function(name,value){attrs[name]=String(value);},
    getBoundingClientRect:function(){return {height:120};},
    compareDocumentPosition:function(other){return order < other.order ? 4 : 2;},
    closest:function(){return null;}
  };
}
function pause(){return new Promise(function(resolve){setTimeout(resolve,10);});}
(async function(){
  const frames=[frame(1),frame(2),frame(3),frame(4)];
  const config={lifecycleVersion:5,maxActiveVisualizations:2};
  frames.forEach(function(item){manager.watch(item,config);});
  [frames[3],frames[0],frames[2],frames[1]].forEach(function(item){manager.live(item,config);});
  await pause();
  const reloadStates=frames.map(function(item){return item.getAttribute('data-iv-state');});
  const eventsBeforeRestore=events.length;
  manager.activate(frames[0]);
  const queuedBeforeRestore=frames[0].getAttribute('data-iv-state') === 'queued';
  const restoreStartedSnapshot=events.length !== eventsBeforeRestore;
  await new Promise(resolve=>setTimeout(resolve,60));
  const restoredAfterAdmission=frames[0].getAttribute('srcdoc') === 'original-1';
  manager.live(frames[0],config);
  await pause();
  console.log(JSON.stringify({
    reloadStates:reloadStates,
    restoredStates:frames.map(function(item){return item.getAttribute('data-iv-state');}),
    queuedBeforeRestore:queuedBeforeRestore,
    restoredAfterAdmission:restoredAfterAdmission,
    restoreStartedSnapshot:restoreStartedSnapshot,
    events:events
  }));
})();
"""
    )
    assert result["reloadStates"] == ["static", "static", "live", "live"]
    assert result["restoredStates"] == ["live", "static", "static", "live"]
    assert result["queuedBeforeRestore"] is True
    assert result["restoredAfterAdmission"] is True
    assert result["restoreStartedSnapshot"] is False
    assert len(result["events"]) >= 6
    for index in range(0, len(result["events"]), 2):
        assert result["events"][index].startswith("ready-")
        assert result["events"][index + 1] == result["events"][index].replace(
            "ready-", "snapshot-"
        )


def test_lifecycle_resets_cached_srcdoc_when_spa_reuses_an_iframe_node():
    result = _run_lifecycle_js(
        """
function frame(source, order) {
  const attrs = {srcdoc:source};
  return {
    order:order,isConnected:true,style:{},parentElement:null,
    contentDocument:{title:'Chart'},
    contentWindow:{
      _ivCreateSnapshot:function(){return Promise.resolve({url:'data:image/png;base64,eA=='});}
    },
    getAttribute:function(name){return attrs[name] || '';},
    setAttribute:function(name,value){attrs[name]=String(value);},
    getBoundingClientRect:function(){return {width:600,height:120};},
    getClientRects:function(){return [{}];},
    compareDocumentPosition:function(other){return order < other.order ? 4 : 2;},
    closest:function(){return null;}
  };
}
function pause(){return new Promise(function(resolve){setTimeout(resolve,10);});}
(async function(){
  const reused=frame('old-visualization',1);
  manager.watch(reused,{lifecycleVersion:5,lifecycleKey:'old',maxActiveVisualizations:1});
  manager.live(reused,{lifecycleVersion:5,lifecycleKey:'old',maxActiveVisualizations:1});
  reused.setAttribute('srcdoc','new-visualization');
  manager.watch(reused,{lifecycleVersion:5,lifecycleKey:'new',maxActiveVisualizations:1});
  manager.live(reused,{lifecycleVersion:5,lifecycleKey:'new',maxActiveVisualizations:1});
  const latest=frame('latest-visualization',2);
  manager.watch(latest,{lifecycleVersion:5,lifecycleKey:'latest',maxActiveVisualizations:1});
  manager.live(latest,{lifecycleVersion:5,lifecycleKey:'latest',maxActiveVisualizations:1});
  await pause();
  const suspended=reused.getAttribute('data-iv-state');
  manager.activate(reused);
  await new Promise(resolve=>setTimeout(resolve,60));
  console.log(JSON.stringify({
    suspended:suspended,
    restored:reused.getAttribute('srcdoc'),
    state:reused.getAttribute('data-iv-state')
  }));
})();
"""
    )
    assert result == {
        "suspended": "static",
        "restored": "new-visualization",
        "state": "loading",
    }


def test_lifecycle_releases_live_frame_even_with_missing_preview():
    result = _run_lifecycle_js(
        """
function frame(order, snapshot) {
  const attrs = {srcdoc:'visualization-' + order};
  return {
    order:order,isConnected:true,style:{},parentElement:null,
    contentDocument:{title:'Chart ' + order},
    contentWindow:{_ivCreateSnapshot:function(){return Promise.resolve(snapshot);}},
    getAttribute:function(name){return attrs[name] || '';},
    setAttribute:function(name,value){attrs[name]=String(value);},
    getBoundingClientRect:function(){return {width:600,height:120};},
    getClientRects:function(){return [{}];},
    compareDocumentPosition:function(other){return order < other.order ? 4 : 2;},
    closest:function(){return null;}
  };
}
function pause(){return new Promise(function(resolve){setTimeout(resolve,10);});}
(async function(){
  const missing=frame(1,null);
  const available=frame(2,{url:'data:image/png;base64,eA=='});
  const config={lifecycleVersion:5,maxActiveVisualizations:1};
  [missing,available].forEach(function(item){manager.watch(item,config);manager.live(item,config);});
  await pause();
  console.log(JSON.stringify({
    missingState:missing.getAttribute('data-iv-state'),
    hasRestore:missing.getAttribute('srcdoc').includes('Restore interactivity'),
    availableState:available.getAttribute('data-iv-state')
  }));
})();
"""
    )
    assert result == {
        "missingState": "static",
        "hasRestore": True,
        "availableState": "live",
    }


def test_lifecycle_limit_counts_iframes_hidden_by_spa_chat_switching():
    result = _run_lifecycle_js(
        """
function frame(order, visible) {
  const attrs = {srcdoc:'visualization-' + order};
  return {
    order:order,isConnected:true,style:{},parentElement:null,
    contentDocument:{title:'Chart ' + order},
    contentWindow:{_ivCreateSnapshot:function(){return Promise.resolve({url:'data:image/png;base64,eA=='});}},
    getAttribute:function(name){return attrs[name] || '';},
    setAttribute:function(name,value){attrs[name]=String(value);},
    getBoundingClientRect:function(){return visible ? {width:600,height:120} : {width:0,height:0};},
    getClientRects:function(){return visible ? [{}] : [];},
    compareDocumentPosition:function(other){return order < other.order ? 4 : 2;},
    closest:function(){return null;}
  };
}
function pause(){return new Promise(function(resolve){setTimeout(resolve,10);});}
(async function(){
  const hidden=frame(1,false);
  const first=frame(2,true);
  const second=frame(3,true);
  const config={lifecycleVersion:5,maxActiveVisualizations:1};
  [hidden,first,second].forEach(function(item){manager.watch(item,config);manager.live(item,config);});
  await pause();
  console.log(JSON.stringify({
    hidden:hidden.getAttribute('data-iv-state'),
    first:first.getAttribute('data-iv-state'),
    second:second.getAttribute('data-iv-state')
  }));
})();
"""
    )
    assert result == {"hidden": "static", "first": "static", "second": "live"}


def test_zero_active_limit_disables_lifecycle_suspension():
    result = _run_lifecycle_js(
        """
function frame(order) {
  const attrs = {srcdoc:'visualization-' + order};
  return {
    order:order,isConnected:true,style:{},parentElement:null,
    contentDocument:{title:'Chart ' + order},
    contentWindow:{_ivCreateSnapshot:function(){return Promise.resolve({url:'data:image/png;base64,eA=='});}},
    getAttribute:function(name){return attrs[name] || '';},
    setAttribute:function(name,value){attrs[name]=String(value);},
    getBoundingClientRect:function(){return {width:600,height:120};},
    getClientRects:function(){return [{}];},
    compareDocumentPosition:function(other){return order < other.order ? 4 : 2;},
    closest:function(){return null;}
  };
}
function pause(){return new Promise(function(resolve){setTimeout(resolve,10);});}
(async function(){
  const frames=[frame(1),frame(2),frame(3),frame(4)];
  const config={lifecycleVersion:5,maxActiveVisualizations:0};
  frames.forEach(function(item){manager.watch(item,config);manager.live(item,config);});
  await pause();
  console.log(JSON.stringify(frames.map(function(item){return item.getAttribute('data-iv-state');})));
})();
"""
    )
    assert result == ["live", "live", "live", "live"]


def test_snapshot_readiness_waits_for_scripts_and_fade_animation():
    source = iv.STREAMING_OBSERVER_SCRIPT
    assert "window.__ivSnapshotReady = function()" in source
    assert "Promise.resolve(_ivScriptChain)" in source
    assert "if (_ivSnapshotReadyPromise) return _ivSnapshotReadyPromise" in source
    assert "}, 1100);" in source
    lifecycle = iv.LIFECYCLE_BOOTSTRAP_SCRIPT
    assert lifecycle.index("return ready();") < lifecycle.index("return creator();")


def test_live_stream_poll_survives_observer_attachment_until_finalize():
    source = iv.STREAMING_OBSERVER_SCRIPT
    assert "if (finalized || innerObserver)" not in source
    assert "if (!finalized && !innerObserver)" not in source
    assert "if (finalized) {" in source
    assert "if (!finalized) pollInterval = setInterval(pollTick, 400);" in source
    assert "var observedMessage = null;" in source
    assert "if (innerObserver && observedMessage === msg) return;" in source
    assert "observedMessage = msg;" in source
    assert source.index("stopLocalWatchers();") < source.index(
        "window.__ivLifecycleLive"
    )


def test_live_stream_uses_server_message_identity_before_dom_ancestry():
    source = iv.STREAMING_OBSERVER_SCRIPT
    configured_start = source.index("function configuredMessage()")
    finder_start = source.index("function findMyMessage()")
    finder_end = source.index("function determineIndex()", finder_start)
    finder = source[finder_start:finder_end]
    assert configured_start < finder_start
    assert "parent.document.getElementById('message-' + messageId)" in source
    assert finder.index("var configured = configuredMessage();") < finder.index(
        "var frame = window.frameElement;"
    )
    context_start = source.index("function _ivChatContext()")
    context_end = source.index("function _ivStartRecovery", context_start)
    context = source[context_start:context_end]
    assert "cfg.chatId" in context
    assert "cfg.messageId" in context


def test_external_libraries_preload_and_ready_waits_for_script_chain():
    source = iv.STREAMING_OBSERVER_SCRIPT
    assert "function enqueueExternalScripts(html)" in source
    assert "enqueueExternalScripts(raw);" in source
    assert source.index("enqueueExternalScripts(raw);") < source.index(
        "var cut = findSafeCut(raw);"
    )
    assert "function _ivLoadExternalScript(src, attrs, attempt, optional)" in source
    assert "if (attempt < 2 && !window.__ivDisposed)" in source
    assert "failed to load a required chart library" in source
    assert "_ivRecovery === 'failed' || _ivScriptFailures.length > 0" in source
    finalize_start = source.index("function finalize(fullText)")
    finalize_end = source.index("function isBlockClosed()", finalize_start)
    finalize_source = source[finalize_start:finalize_end]
    assert finalize_source.index("renderSafeInto(fullText, true);") < finalize_source.index(
        "Promise.resolve(_ivScriptChain)"
    )
    assert finalize_source.index("Promise.resolve(_ivScriptChain)") < finalize_source.index(
        "_ivWaitForFirstPaint()"
    )
    assert finalize_source.index("_ivWaitForFirstPaint()") < finalize_source.index(
        "hideLoader();"
    )
    assert finalize_source.index("hideLoader();") < finalize_source.index(
        "window.__ivLifecycleLive"
    )
    assert finalize_source.index("window.__ivLifecycleLive") < finalize_source.index(
        "if (wasStreaming)"
    )


def test_canvas_first_paint_is_nudged_before_ready():
    source = iv.STREAMING_OBSERVER_SCRIPT
    assert "Inline modules execute asynchronously" in source
    assert "moduleEl.onload = moduleEl.onerror" in source
    assert "function _ivDispatchResize()" in source
    assert "window.dispatchEvent(event);" in source
    assert "window.Chart && window.Chart.instances" in source
    assert "window.echarts.getInstanceByDom" in source
    assert "window.Plotly.Plots.resize" in source
    assert "function _ivHasPaintedCanvas()" in source
    assert "function _ivWaitForFirstPaint()" in source
    assert "elapsed >= 2000" in source


def test_chart_scripts_wait_for_visible_layout_and_refresh_after_chat_switch():
    source = iv.STREAMING_OBSERVER_SCRIPT
    assert "function _ivWaitForUsableLayout()" in source
    assert source.index("return _ivWaitForUsableLayout();") < source.index(
        "function createInlineScript()"
    )
    assert "window.__ivRefreshLayout = function()" in source
    assert "if (!finalized || !_ivHasUsableLayout()) return;" in source
    lifecycle = iv.LIFECYCLE_BOOTSTRAP_SCRIPT
    assert "record.layoutObserver = new ResizeObserver" in lifecycle
    assert "frame.contentWindow.__ivRefreshLayout" in lifecycle


def test_snapshot_rejects_blank_rasters_and_prefers_dominant_canvas():
    source = iv.BODY_SCRIPTS
    assert "function canvasLooksBlank(canvas)" in source
    assert "if (canvasLooksBlank(out))" in source
    assert "dominantArea / Math.max(1, pageWidth * pageHeight) >= 0.55" in source


def test_all_iframe_scripts_keep_the_srcdoc_safety_invariant():
    for name, source in iv._IFRAME_EMBEDDED_SCRIPTS.items():
        iv._assert_srcdoc_safe(name, source)


def _run_startup_loader_js(scenario, config=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executable browser-helper tests")
    source = iv.STREAMING_OBSERVER_SCRIPT
    start = source.index("  var _ivScriptChain = Promise.resolve();")
    end = source.index("  // Start fetching referenced libraries", start)
    harness = r"""
const assert = require('node:assert/strict');
const appended = [];
const timers = new Map();
let nextTimer = 0;
global.setTimeout = (callback, ms) => {
  const id = ++nextTimer;
  timers.set(id, {callback, ms});
  return id;
};
global.clearTimeout = id => timers.delete(id);
function fireTimer(ms) {
  const entry = [...timers].find(([, timer]) => timer.ms === ms);
  assert.ok(entry, 'expected timer: ' + ms);
  timers.delete(entry[0]);
  entry[1].callback();
}
const document = {
  baseURI: 'https://webui.example/chat/123',
  documentElement: {clientWidth: 800},
  createElement() {
    return {
      attributes: [], textContent: '',
      setAttribute(name, value) { this.attributes.push({name, value}); },
      getAttribute(name) {
        const attr = this.attributes.find(a => a.name === name);
        return attr ? attr.value : null;
      }
    };
  },
  head: {
    appendChild(el) { el.parentNode = this; appended.push(el); },
    removeChild(el) { el.parentNode = null; }
  }
};
function incoming(src, code = '') {
  const el = document.createElement('script');
  if (src) el.setAttribute('src', src);
  el.textContent = code;
  return el;
}
async function drain() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
"""
    completed = subprocess.run(
        [node],
        input=(
            harness
            + "\nconst window = {__ivRuntimeConfig: "
            + json.dumps(config or {})
            + "};\n"
            + source[start:end]
            + "\n(async () => {\n"
            + scenario
            + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("first_loaded", [0, 1])
@pytest.mark.parametrize("custom_urls", [False, True])
def test_startup_loads_both_libraries_immediately_and_waits_without_duplicates(
    first_loaded, custom_urls
):
    config = (
        {"chartjsUrl": "/assets/chart.js", "plotlyUrl": "/assets/plotly.js"}
        if custom_urls else {}
    )
    urls = [
        config.get("chartjsUrl", "/static/chart.umd.min.js"),
        config.get("plotlyUrl", "/static/plotly.umd.min.js"),
    ]
    _run_startup_loader_js(
        "const urls = " + json.dumps(urls) + ";\n"
        + "const first = " + str(first_loaded) + ";\n"
        + r"""
// No generated markup has been enqueued: both requests already started.
assert.deepEqual(appended.map(el => el.getAttribute('src')), urls);
for (const url of urls) {
  enqueueScript(incoming(url));
  enqueueScript(incoming(new URL(url, document.baseURI).href));
}
enqueueScript(incoming(null, 'Chart.render(); Plotly.render();'));
await drain();
assert.equal(appended.length, 2);
appended[first].onload();
await drain();
assert.equal(appended.length, 2, 'inline must wait for the slower library');
appended[1 - first].onload();
await _ivScriptChain;
assert.equal(appended.length, 3);
assert.equal(appended[2].textContent, 'Chart.render(); Plotly.render();');
assert.deepEqual(_ivScriptFailures, []);
assert.equal(timers.size, 0);
""",
        config,
    )


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_startup_library_failure_retries_and_blocks_inline_code(failure):
    _run_startup_loader_js(
        "const failure = " + json.dumps(failure) + ";\n"
        + r"""
enqueueScript(incoming(null, 'Chart.render();'));
appended[1].onload();
function fail(script) {
  if (failure === 'timeout') fireTimer(12000);
  else script.onerror();
}
fail(appended[0]);
fireTimer(300);
assert.equal(appended.length, 3);
fail(appended[2]);
fireTimer(600);
assert.equal(appended.length, 4);
fail(appended[3]);
await _ivScriptChain;
assert.equal(_ivScriptFailures.length, 1);
assert.ok(_ivScriptFailures[0].includes('chartjs'));
assert.equal(appended.length, 4, 'inline code must not run after failure');
assert.equal(timers.size, 0, 'retries must be bounded');
"""
    )


def test_startup_retry_success_unblocks_additional_imports_then_inline_code():
    _run_startup_loader_js(r"""
enqueueScript(incoming('/assets/d3.js'));
enqueueScript(incoming(null, 'Chart.render();'));
appended[0].onerror();
appended[1].onload();
await drain();
assert.equal(appended.length, 3);
fireTimer(300);
appended[3].onload();
await drain();
assert.equal(appended.length, 4);
assert.equal(appended[2].getAttribute('src'), '/assets/d3.js');
appended[2].onload();
await _ivScriptChain;
assert.equal(appended.length, 5);
assert.equal(appended[4].textContent, 'Chart.render();');
assert.deepEqual(_ivScriptFailures, []);
assert.equal(timers.size, 0);
""")


def test_optional_plotly_failure_does_not_delay_or_break_chartjs_or_plain_code():
    _run_startup_loader_js(r"""
enqueueScript(incoming(null, 'plainCode();'));
enqueueScript(incoming(null, 'Chart.render();'));
await drain();
assert.equal(appended[2].textContent, 'plainCode();');
appended[0].onload();
await _ivScriptChain;
assert.equal(appended[3].textContent, 'Chart.render();');
appended[1].onerror(); fireTimer(300);
appended[4].onerror(); fireTimer(600);
appended[5].onerror();
await drain();
assert.deepEqual(_ivScriptFailures, []);
assert.equal(timers.size, 0);
""")


def test_explicit_dependencies_support_aliases_and_failure_is_per_consumer():
    _run_startup_loader_js(r"""
const first = incoming(null, 'alias.render();');
first.setAttribute('data-iv-libraries', 'plotly');
enqueueScript(first);
enqueueScript(incoming(null, 'Chart.render();'));
appended[0].onload();
appended[1].onerror(); fireTimer(300);
appended[2].onerror(); fireTimer(600);
appended[3].onerror();
await _ivScriptChain;
assert.equal(appended.length, 5);
assert.equal(appended[4].textContent, 'Chart.render();');
assert.equal(_ivScriptFailures.length, 1);
""")


def test_layout_gate_rejects_hidden_frame_and_keeps_only_one_poll_timer():
    _run_startup_loader_js(r"""
appended[0].onload(); appended[1].onload();
let visible = false, resized, disconnected = false;
window.frameElement = {isConnected:true,
  getBoundingClientRect:()=>({width:visible?600:0,height:visible?200:0})};
global.ResizeObserver = class {
  constructor(callback) { resized = callback; }
  observe() {}
  disconnect() { disconnected = true; }
};
assert.equal(_ivHasUsableLayout(), false);
const ready = _ivWaitForUsableLayout();
resized(); resized(); resized();
assert.equal(timers.size, 1);
visible = true;
resized();
await ready;
assert.equal(timers.size, 0);
assert.equal(disconnected, true);
visible = false;
resized();
assert.equal(timers.size, 0, 'settled observer cannot restart polling');
""")


def test_heavy_runtime_is_inert_until_admitted():
    html = iv._build_html(tool_data_bridge=iv._build_tool_data_bridge({"value": 42}))
    start = html.index('<template id="iv-runtime">')
    end = html.index('</template>', start)
    assert start < html.index('window.getToolData=') < end
    assert start < html.index('var _ivLibraryLoads') < end
    assert start < html.index('var themeObserver') < end
    assert html.index('function installLifecycleManager()') > end


def test_admission_starts_only_two_of_thirty_and_cleans_up_on_page_exit():
    result = _run_lifecycle_js(r"""
(async()=>{
  const frames=Array.from({length:30},(_,i)=>makeTestFrame(i));
  const config={lifecycleVersion:5,maxActiveVisualizations:2};
  const starts=[];
  frames.forEach(f=>{
    manager.watch(f,config);
    manager.requestStart(f,config,()=>{starts.push(f.order);manager.live(f,config);});
  });
  await new Promise(r=>setTimeout(r,180));
  const stats=manager.stats();
  parentHandlers.pagehide({persisted:false});
  console.log(JSON.stringify({starts,stats,after:manager.stats()}));
})();
""")
    assert result["starts"] == [29, 28]
    assert result["stats"]["states"] == {"static": 28, "live": 2}
    assert result["stats"]["sourceBytes"] > 0  # bounded fallback without IndexedDB
    assert result["after"] == {"states": {}, "sourceBytes": 0, "previewBytes": 0}


def test_preview_cache_evicts_images_but_keeps_restore_sources():
    result = _run_lifecycle_js(r"""
(async()=>{
  const config={lifecycleVersion:5,maxActiveVisualizations:1};
  const frames=Array.from({length:5},(_,i)=>makeTestFrame(i,'source-'+i,
    {url:'data:image/png;base64,eA==',width:2000,height:2000}));
  frames.forEach(f=>{manager.watch(f,config);manager.live(f,config);});
  await new Promise(r=>setTimeout(r,30));
  const stats=manager.stats();
  const previewCount=frames.filter(f=>f.getAttribute('srcdoc').includes('<img')).length;
  manager.activate(frames[0]);
  await new Promise(r=>setTimeout(r,60));
  console.log(JSON.stringify({stats,previewCount,restored:frames[0].getAttribute('srcdoc')}));
})();
""")
    assert result["stats"]["previewBytes"] <= 16 * 1024 * 1024
    assert result["previewCount"] == 1
    assert result["restored"] == "source-0"


def test_admission_waits_for_old_iframe_document_to_unload():
    result = _run_lifecycle_js(r"""
(async()=>{
  const config={lifecycleVersion:5,maxActiveVisualizations:1};
  const first=makeTestFrame(0), second=makeTestFrame(1);
  let starts=0, loaded;
  first.addEventListener=(name,callback)=>{if(name==='load')loaded=callback;};
  first.removeEventListener=()=>{};
  manager.watch(first,config);
  manager.requestStart(first,config,()=>{starts++;manager.live(first,config);});
  await new Promise(r=>setTimeout(r,130));
  manager.watch(second,config);
  manager.requestStart(second,config,()=>{starts++;manager.live(second,config);});
  await new Promise(r=>setTimeout(r,130));
  const before={starts,states:manager.stats().states};
  first.contentDocument.documentElement={getAttribute:()=> '5'};
  loaded();
  await new Promise(r=>setTimeout(r,30));
  console.log(JSON.stringify({before,after:{starts,states:manager.stats().states}}));
})();
""")
    assert result["before"] == {"starts": 1, "states": {"suspending": 1, "queued": 1}}
    assert result["after"] == {"starts": 2, "states": {"static": 1, "live": 1}}


def test_storage_failure_preserves_source_and_does_not_admit_over_budget():
    result = _run_lifecycle_js(r"""
(async()=>{
  const config={lifecycleVersion:5,maxActiveVisualizations:1};
  const first=makeTestFrame(0,'x'.repeat(5*1024*1024));
  const second=makeTestFrame(1);
  let starts=0;
  manager.watch(first,config);
  manager.requestStart(first,config,()=>{starts++;manager.live(first,config);});
  await new Promise(r=>setTimeout(r,130));
  manager.watch(second,config);
  manager.requestStart(second,config,()=>{starts++;manager.live(second,config);});
  await new Promise(r=>setTimeout(r,130));
  console.log(JSON.stringify({starts,stats:manager.stats(),sourceLength:first.getAttribute('srcdoc').length}));
})();
""")
    assert result["starts"] == 1
    assert result["stats"]["states"] == {"live": 1, "queued": 1}
    assert result["stats"]["sourceBytes"] == 0
    assert result["sourceLength"] == 5 * 1024 * 1024


def test_unmount_prunes_shared_text_nodes_and_disposes_child():
    result = _run_lifecycle_js(r"""
const f=makeTestFrame(1);
let disposed=0;
f.contentWindow.__ivDispose=()=>disposed++;
manager.watch(f,{lifecycleVersion:5,maxActiveVisualizations:2});
const stale={isConnected:false}, current={isConnected:true};
parentWindow.__ivChatBlankedNodes=[stale,current];
parentWindow.__ivChatOriginalText=new WeakMap([[stale,'large payload']]);
parentWindow.__ivChatBlankedSet=new WeakSet([stale,current]);
f.isConnected=false;
observers[0].callback([]);
console.log(JSON.stringify({disposed,remaining:parentWindow.__ivChatBlankedNodes.length,stats:manager.stats()}));
""")
    assert result == {"disposed": 1, "remaining": 1, "stats": {"states": {}, "sourceBytes": 0, "previewBytes": 0}}


def test_theme_observer_disconnects_exactly_once_on_dispose():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    scripts = [re.fullmatch(r"\s*<script>\s*(.*?)\s*</script>\s*", s, re.DOTALL).group(1)
               for s in [iv.CLEANUP_SCRIPT, iv.THEME_DETECTION_SCRIPT]]
    completed = subprocess.run([node], input=r"""
const assert=require('node:assert/strict');
let disconnected=0;
const root={classList:{contains:()=>false},getAttribute:()=> 'light'};
const document={documentElement:root};
const parent={document};
const window={addEventListener(){}};
const getComputedStyle=()=>({colorScheme:'light'});
class MutationObserver { observe(){} disconnect(){disconnected++;} }
""" + "\n".join(scripts) + r"""
window.__ivDispose(); window.__ivDispose();
assert.equal(disconnected,1);
assert.equal(window.__ivDisposed,true);
""", text=True, capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        iv.DOWNSAMPLING_SCRIPT,
        iv.CLEANUP_SCRIPT,
        iv.LIFECYCLE_BOOTSTRAP_SCRIPT,
        iv.BODY_SCRIPTS,
        iv.STREAMING_OBSERVER_SCRIPT,
        iv.SAVED_VISUALIZATION_SCRIPT,
    ],
    ids=["downsampling", "cleanup", "lifecycle", "body-scripts", "streaming-observer", "saved-renderer"],
)
def test_new_browser_scripts_parse_after_python_string_decoding(source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executable browser-helper tests")
    match = re.fullmatch(r"\s*<script>\s*(.*?)\s*</script>\s*", source, re.DOTALL)
    assert match is not None
    completed = subprocess.run(
        [node],
        input=f"new Function({json.dumps(match.group(1))});",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
