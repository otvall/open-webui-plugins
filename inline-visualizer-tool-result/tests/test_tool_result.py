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


def response_output(*calls, name="execute_sql"):
    output = []
    for call_id, result in calls:
        output.append(
            {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": name,
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


def tool_message(call_id, result, name="execute_sql"):
    return {
        "role": "tool",
        "name": name,
        "tool_call_id": call_id,
        "content": (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        ),
    }


def resolve_messages(messages, name="execute_sql"):
    return run(iv._resolve_tool_result(name, None, {}, messages))


def extract_tool_json(html):
    match = re.search(
        r'<script id="iv-tool-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def capture_visualize(tool, **kwargs):
    events = []

    async def emitter(event):
        events.append(event)

    returned = run(tool.visualize_tool_result(__event_emitter__=emitter, **kwargs))
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


def test_find_output_result_selects_latest_named_call():
    output = response_output(
        ("call-same", [{"version": 1}]),
        ("call-other", [{"other": True}]),
        ("call-same", [{"version": 2}]),
    )

    found, result = resolve_messages(output)

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

    assert resolve_messages(output) == (False, None)


def test_find_message_result_accepts_named_results_without_calls():
    messages = [
        tool_message("call-same", [{"version": 1}]),
        tool_message("call-other", [{"other": True}]),
        tool_message("call-latest", [{"version": 2}]),
    ]

    found, result = resolve_messages(messages)

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

    found, result = resolve_messages(messages)

    assert found is True
    assert result == [{"source": "latest"}]


def test_find_message_result_accepts_direct_responses_api_item():
    messages = [
        {
            "type": "function_call_output",
            "name": "execute_sql",
            "call_id": "call_provider_generated",
            "output": [{"type": "output_text", "text": '{"ok":true}'}],
            "status": "completed",
        }
    ]

    found, result = resolve_messages(messages)

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
            "execute_sql",
            DummyRequest(),
            {"chat_id": "chat", "message_id": "assistant-message"},
            [tool_message("call-data", [{"source": "messages"}])],
        )
    )

    assert found is True
    assert result == [{"source": "active"}]


def test_resolve_result_falls_back_to_messages(monkeypatch):
    async def load_outputs(request, metadata):
        return [response_output(("call-other", [{"other": True}]), name="other_tool")]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)

    found, result = run(
        iv._resolve_tool_result(
            "execute_sql",
            DummyRequest(),
            {},
            [tool_message("call-data", [{"source": "messages"}])],
        )
    )

    assert found is True
    assert result == [{"source": "messages"}]


def test_source_tool_name_is_required_in_public_signature():
    parameter = inspect.signature(iv.Tools.visualize_tool_result).parameters[
        "source_tool_name"
    ]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str
    assert "source_tool_call_id" not in inspect.signature(
        iv.Tools.visualize_tool_result
    ).parameters


def test_retry_attempt_is_bounded_in_public_signature():
    parameter = inspect.signature(iv.Tools.visualize_tool_result).parameters[
        "retry_attempt"
    ]

    assert parameter.default == 0
    assert parameter.annotation == iv.Literal[0, 1]


def test_visualize_without_event_emitter_returns_html_response_tuple():
    response, context = run(
        iv.Tools().visualize_tool_result(
            source_tool_name="execute_sql",
            title="Fallback",
            __messages__=[tool_message("call-data", [{"x": 1}])],
            __metadata__={"chat_id": "chat/live", "message_id": "msg:live"},
        )
    )

    assert isinstance(response, iv.HTMLResponse)
    assert response.headers["content-disposition"] == "inline"
    assert "waiting for content" in context
    assert b"getToolData" in response.body
    assert b'tool-result-1.2.0' in response.body
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
        iv.Tools().visualize_tool_result(
            source_tool_name="execute_sql",
            title="Dual path",
            __messages__=[tool_message("call-data", [{"x": 1}])],
            __event_emitter__=emitter,
        )
    )

    assert isinstance(response, iv.HTMLResponse)
    assert "waiting for content" in context
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
        tool_message("call-a", [{"marker": "DO_NOT_INCLUDE_A"}], name="other_tool"),
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
        tool_message("call-b", [{"marker": "ONLY_SELECTED_B"}], name=None),
        tool_message("call-c", [{"marker": "DO_NOT_INCLUDE_C"}], name="other_tool"),
    ]

    result, html, _ = capture_visualize(
        iv.Tools(),
        title="Selected",
        source_tool_name="execute_sql",
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


@pytest.mark.parametrize("source_tool_name", ["", "   "])
def test_empty_source_tool_name_is_a_controlled_error(source_tool_name):
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_name=source_tool_name,
        __messages__=[],
    )

    assert result["status"] == "error"
    assert result["error"] == "Invalid source_tool_name"
    assert result["source_tool_name"] == source_tool_name
    assert html is None
    assert events == []


def test_unknown_or_unfinished_source_tool_name_requests_one_retry(
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
        source_tool_name="execute_sql",
        __messages__=[],
    )

    assert result["status"] == "retry_required"
    assert result["code"] == "source_result_not_visible_yet"
    assert result["source_tool_name"] == "execute_sql"
    assert result["retry"] == {
        "tool": "visualize_tool_result",
        "arguments": {
            "source_tool_name": "execute_sql",
            "title": "Tool Result Visualization",
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
        source_tool_name="execute_sql",
        title="Retry",
        retry_attempt=1,
        __messages__=[],
    )

    assert result["status"] == "error"
    assert result["error"] == "Tool result not found"
    assert result["source_tool_name"] == "execute_sql"
    assert "single visualization retry" in result["message"]
    assert "do not rerun the data-producing tool" in result["message"]
    assert html is None
    assert events == []


def test_retry_succeeds_when_source_result_becomes_visible():
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_name="execute_sql",
        title="Retry success",
        retry_attempt=1,
        __messages__=[tool_message("call-data", [{"value": 42}])],
    )

    assert "waiting for content" in result
    assert extract_tool_json(html) == [{"value": 42}]
    assert len(events) == 1


def test_invalid_retry_attempt_is_a_controlled_error():
    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_name="execute_sql",
        retry_attempt=2,
        __messages__=[tool_message("call-data", [{"value": 42}])],
    )

    assert result["status"] == "error"
    assert result["error"] == "Invalid retry_attempt"
    assert html is None
    assert events == []


def test_unserializable_result_is_a_controlled_error(monkeypatch):
    async def resolve(tool_name, request, metadata, messages):
        return True, float("nan")

    monkeypatch.setattr(iv, "_resolve_tool_result", resolve)

    result, html, events = capture_visualize(
        iv.Tools(),
        source_tool_name="execute_sql",
    )

    assert result["status"] == "error"
    assert result["error"] == "Tool result cannot be serialized"
    assert result["source_tool_name"] == "execute_sql"
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
class FakeMutationObserver {{ constructor(callback) {{ this.callback = callback; }} observe() {{}} }}
function raf(callback) {{ callback(); return 1; }}
const parentWindow = {{}};
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
)(childWindow,parentWindow,{{}},FakeMutationObserver,raf);
const manager = parentWindow.__ivLifecycleV3;
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

    html = iv._build_html(
        max_active_visualizations=4,
        point_density=1.5,
    )
    config_match = re.search(
        r'<script id="iv-runtime-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert config_match is not None
    assert json.loads(config_match.group(1)) == {
        "build": "tool-result-1.2.0",
        "lifecycleVersion": 3,
        "maxActiveVisualizations": 4,
        "pointDensity": 1.5,
    }


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
    assert "window.__ivLifecycleV3" in source


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
  const config={lifecycleVersion:3,maxActiveVisualizations:2};
  frames.forEach(function(item){manager.watch(item,config);});
  [frames[3],frames[0],frames[2],frames[1]].forEach(function(item){manager.live(item,config);});
  await pause();
  const reloadStates=frames.map(function(item){return item.getAttribute('data-iv-state');});
  const eventsBeforeRestore=events.length;
  manager.activate(frames[0]);
  const restoredImmediately=frames[0].getAttribute('srcdoc') === 'original-1' &&
    frames[0].getAttribute('data-iv-state') === 'activating';
  const restoreStartedSnapshot=events.length !== eventsBeforeRestore;
  manager.live(frames[0],config);
  await pause();
  console.log(JSON.stringify({
    reloadStates:reloadStates,
    restoredStates:frames.map(function(item){return item.getAttribute('data-iv-state');}),
    restoredImmediately:restoredImmediately,
    restoreStartedSnapshot:restoreStartedSnapshot,
    events:events
  }));
})();
"""
    )
    assert result["reloadStates"] == ["static", "static", "live", "live"]
    assert result["restoredStates"] == ["live", "static", "static", "live"]
    assert result["restoredImmediately"] is True
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
  manager.watch(reused,{lifecycleVersion:3,lifecycleKey:'old',maxActiveVisualizations:1});
  manager.live(reused,{lifecycleVersion:3,lifecycleKey:'old',maxActiveVisualizations:1});
  reused.setAttribute('srcdoc','new-visualization');
  manager.watch(reused,{lifecycleVersion:3,lifecycleKey:'new',maxActiveVisualizations:1});
  manager.live(reused,{lifecycleVersion:3,lifecycleKey:'new',maxActiveVisualizations:1});
  const latest=frame('latest-visualization',2);
  manager.watch(latest,{lifecycleVersion:3,lifecycleKey:'latest',maxActiveVisualizations:1});
  manager.live(latest,{lifecycleVersion:3,lifecycleKey:'latest',maxActiveVisualizations:1});
  await pause();
  const suspended=reused.getAttribute('data-iv-state');
  manager.activate(reused);
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
        "state": "activating",
    }


def test_lifecycle_never_replaces_a_live_frame_with_a_missing_preview():
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
  const config={lifecycleVersion:3,maxActiveVisualizations:1};
  [missing,available].forEach(function(item){manager.watch(item,config);manager.live(item,config);});
  await pause();
  console.log(JSON.stringify({
    missingState:missing.getAttribute('data-iv-state'),
    missingSource:missing.getAttribute('srcdoc'),
    availableState:available.getAttribute('data-iv-state')
  }));
})();
"""
    )
    assert result == {
        "missingState": "live",
        "missingSource": "visualization-1",
        "availableState": "static",
    }


def test_lifecycle_limit_ignores_iframes_hidden_by_spa_chat_switching():
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
  const config={lifecycleVersion:3,maxActiveVisualizations:1};
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
    assert result == {"hidden": "live", "first": "static", "second": "live"}


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
  const config={lifecycleVersion:3,maxActiveVisualizations:0};
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


def test_snapshot_rejects_blank_rasters_and_prefers_dominant_canvas():
    source = iv.BODY_SCRIPTS
    assert "function canvasLooksBlank(canvas)" in source
    assert "if (canvasLooksBlank(out))" in source
    assert "dominantArea / Math.max(1, pageWidth * pageHeight) >= 0.55" in source


def test_all_iframe_scripts_keep_the_srcdoc_safety_invariant():
    for name, source in iv._IFRAME_EMBEDDED_SCRIPTS.items():
        iv._assert_srcdoc_safe(name, source)


@pytest.mark.parametrize(
    "source",
    [
        iv.DOWNSAMPLING_SCRIPT,
        iv.LIFECYCLE_BOOTSTRAP_SCRIPT,
        iv.BODY_SCRIPTS,
        iv.STREAMING_OBSERVER_SCRIPT,
    ],
    ids=["downsampling", "lifecycle", "body-scripts", "streaming-observer"],
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


def messages_for_format(output, format, message_id="assistant-source"):
    if format == "responses":
        return output
    if format == "saved":
        return [{"role": "assistant", "id": message_id, "output": output}]
    calls = [item for item in output if item["type"] == "function_call"]
    messages = [{
        "role": "assistant",
        "id": message_id,
        "tool_calls": [
            {"id": call["call_id"], "function": {"name": call["name"]}}
            for call in calls
        ],
    }]
    for item in output:
        if item["type"] != "function_call_output":
            continue
        message = {
            "role": "tool",
            "tool_call_id": item["call_id"],
            "status": item.get("status"),
        }
        if "output" in item:
            message["content"] = item["output"]
        messages.append(message)
    return messages


@pytest.mark.parametrize("format", ["responses", "saved", "chat"])
def test_latest_invocation_wins_when_results_complete_out_of_order(format):
    old_call, old_result = response_output(("old", {"version": 1}))
    new_call, new_result = response_output(("new", {"version": 2}))
    other_call, other_result = response_output(("other", "secret"), name="other")
    output = [old_call, new_call, other_call, new_result, other_result, old_result]

    assert resolve_messages(messages_for_format(output, format)) == (
        True, {"version": 2}
    )


@pytest.mark.parametrize("format", ["responses", "saved", "chat"])
@pytest.mark.parametrize(
    "status", ["missing", "in_progress", "pending", "queued", "requires_approval"]
)
def test_latest_pending_invocation_blocks_older_result(format, status):
    output = response_output(("old", {"version": 1}), ("new", {"partial": True}))
    if status == "missing":
        output.pop()
    else:
        output[-1]["status"] = status

    assert resolve_messages(messages_for_format(output, format)) == (False, None)


@pytest.mark.parametrize("format", ["responses", "saved", "chat"])
def test_recycled_call_id_cannot_complete_a_later_pending_turn(format):
    old = response_output(("reused", {"version": "old"}))
    new = response_output(("reused", {"version": "new"}))[:1]
    messages = (
        messages_for_format(old, format, "old-message")
        + [{"role": "user", "content": "Get new data"}]
        + messages_for_format(new, format, "new-message")
    )

    assert resolve_messages(messages) == (False, None)


@pytest.mark.parametrize("name", ["Execute_SQL", "sql", "functions.execute_sql", " execute_sql "])
def test_tool_names_match_exactly(name):
    assert resolve_messages(response_output(("call", 42)), name) == (False, None)


def test_namespaced_function_name_is_supported_without_normalization():
    output = response_output(("call", 42), name="functions.execute_sql")
    assert resolve_messages(output, "functions.execute_sql") == (True, 42)


def test_unnamed_orphan_result_does_not_infer_name_from_id_or_content():
    message = tool_message("functions.execute_sql:0", "execute_sql", name=None)
    assert resolve_messages([message]) == (False, None)


@pytest.mark.parametrize("format", ["responses", "saved", "chat"])
def test_null_result_is_present_but_absent_payload_is_pending(format):
    output = response_output(("call", None))
    assert resolve_messages(messages_for_format(output, format)) == (True, None)
    del output[-1]["output"]
    assert resolve_messages(messages_for_format(output, format)) == (False, None)


def test_pending_live_call_never_falls_back_to_different_stored_or_historical_call(monkeypatch):
    async def load_outputs(request, metadata):
        return [
            response_output(("new", "partial"))[:1],
            response_output(("old", "stale stored")),
        ]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)
    history = messages_for_format(response_output(("older", "stale history")), "saved")
    assert run(iv._resolve_tool_result(
        "execute_sql", None, {"message_id": "assistant-source"}, history
    )) == (False, None)


@pytest.mark.parametrize("include_call", [True, False])
def test_pending_live_call_can_use_same_call_result_from_stored_snapshot(monkeypatch, include_call):
    async def load_outputs(request, metadata):
        stored = response_output(("selected", {"ok": True}))
        if not include_call:
            stored = stored[1:]
        return [response_output(("selected", "partial"))[:1], stored]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)
    assert run(iv._resolve_tool_result("execute_sql", None, {}, [])) == (
        True, {"ok": True}
    )


@pytest.mark.parametrize("history_id", ["current", "earlier", ""])
def test_history_can_complete_live_call_only_in_same_message(monkeypatch, history_id):
    async def load_outputs(request, metadata):
        return [response_output(("reused", "partial"))[:1]]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)
    history = messages_for_format(
        response_output(("reused", {"ok": True})), "saved", history_id
    )
    result = run(iv._resolve_tool_result(
        "execute_sql", None, {"message_id": "current"}, history
    ))
    assert result == ((True, {"ok": True}) if history_id == "current" else (False, None))


def test_saved_output_and_tool_calls_on_same_message_do_not_duplicate_pending_call():
    messages = messages_for_format(response_output(("call", 42)), "saved")
    messages[0]["tool_calls"] = [
        {"id": "call", "function": {"name": "execute_sql"}}
    ]
    assert resolve_messages(messages) == (True, 42)


@pytest.mark.parametrize("completes", [True, False])
def test_retry_of_latest_call_never_renders_old_data(monkeypatch, completes):
    output = response_output(("old", "STALE"), ("new", {"fresh": True}))
    latest_result = output.pop()

    async def load_outputs(request, metadata):
        return [output]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)
    tool = iv.Tools()
    first, html, events = capture_visualize(
        tool, source_tool_name="execute_sql", title="Latest"
    )
    assert first["status"] == "retry_required"
    assert html is None and events == []
    assert first["retry"]["arguments"] == {
        "source_tool_name": "execute_sql", "title": "Latest", "retry_attempt": 1
    }
    if completes:
        output.append(latest_result)
    second, html, events = capture_visualize(tool, **first["retry"]["arguments"])
    if completes:
        assert extract_tool_json(html) == {"fresh": True}
        assert "STALE" not in html
        assert len(events) == 1
    else:
        assert second["error"] == "Tool result not found"
        assert "retry" not in second
        assert html is None and events == []


def test_unknown_tool_name_requests_one_retry():
    result, html, events = capture_visualize(
        iv.Tools(), source_tool_name="unknown",
        __messages__=response_output(("call", 42)),
    )
    assert result["status"] == "retry_required"
    assert result["retry"]["arguments"]["source_tool_name"] == "unknown"
    assert html is None and events == []


def test_tool_calls_can_name_results_stored_in_output_without_declarations():
    messages = messages_for_format(response_output(("call", 42))[1:], "saved")
    messages[0]["tool_calls"] = [
        {"id": "call", "function": {"name": "execute_sql"}}
    ]
    assert resolve_messages(messages) == (True, 42)


def test_active_result_can_use_name_from_stored_call(monkeypatch):
    async def load_outputs(request, metadata):
        return [
            response_output(("call", {"source": "active"}))[1:],
            response_output(("call", {"source": "stored"})),
        ]

    monkeypatch.setattr(iv, "_load_current_message_outputs", load_outputs)
    assert run(iv._resolve_tool_result("execute_sql", None, {}, [])) == (
        True, {"source": "active"}
    )
