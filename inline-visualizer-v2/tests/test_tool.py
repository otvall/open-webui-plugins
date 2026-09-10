import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tool.py"
SPEC = importlib.util.spec_from_file_location("inline_visualizer_v2_tool", MODULE_PATH)
iv = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(iv)

CACHE_MODULE_PATH = Path(__file__).parents[1] / "cache_tool.py"
CACHE_SPEC = importlib.util.spec_from_file_location(
    "inline_visualizer_v2_cache_tool", CACHE_MODULE_PATH
)
cache_iv = importlib.util.module_from_spec(CACHE_SPEC)
assert CACHE_SPEC.loader is not None
CACHE_SPEC.loader.exec_module(cache_iv)


class DummyRequest:
    def __init__(self, app=None):
        self.state = SimpleNamespace()
        self.app = app or SimpleNamespace(state=SimpleNamespace())


def run(awaitable):
    return asyncio.run(awaitable)


def history(*calls):
    tool_calls = []
    results = []
    for call_id, name, arguments, result in calls:
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
        results.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
    return [{"role": "assistant", "content": "", "tool_calls": tool_calls}, *results]


def response_output(*calls):
    output = []
    for call_id, name, arguments, result in calls:
        output.extend(
            [
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": [
                        {
                            "type": "input_text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ],
                    "status": "completed",
                },
            ]
        )
    return output


_DEFAULT_RESULT = object()


def cache_entry(cache_id="cache-b", result=_DEFAULT_RESULT, tool_id="execute_sql"):
    return {
        "cache_id": cache_id,
        "tool_id": tool_id,
        "tool_call_id": f"call-{cache_id}",
        "arguments": {"sql": "SELECT secret"},
        "result": (
            [{"marker": "ONLY_SELECTED_B"}]
            if result is _DEFAULT_RESULT
            else result
        ),
    }


def extract_cached_json(html):
    match = re.search(
        r'<script id="iv-cached-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def capture_visualize(tool, **kwargs):
    events = []

    async def emitter(event):
        events.append(event)

    result = run(tool.visualize(__event_emitter__=emitter, **kwargs))
    html = events[0]["data"]["embeds"][0] if events else None
    return result, html


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ([{"x": 1}], [{"x": 1}]),
        ({"x": 1}, {"x": 1}),
        ('[{"x":1}]', [{"x": 1}]),
        ('{"x":1}', {"x": 1}),
        ("plain text", "plain text"),
        (None, None),
    ],
)
def test_normalize_cached_result(source, expected):
    assert cache_iv._normalize_cached_result(source) == expected


def test_cache_and_visualizer_are_separate_tool_classes():
    visualizer = iv.Tools()
    cache = cache_iv.Tools()

    assert hasattr(visualizer, "visualize")
    assert not hasattr(visualizer, "cache_tool_call")
    assert hasattr(cache, "cache_tool_call")
    assert not hasattr(cache, "visualize")


def test_cache_tool_call_uses_latest_matching_call_and_tool_call_id():
    request = DummyRequest()
    messages = [
        *history(("call-old", "execute_sql", {"sql": "old"}, [{"v": "old"}])),
        *history(("call-other", "other_tool", {}, [{"v": "other"}])),
        *history(("call-new", "execute_sql", {"sql": "new"}, [{"v": "new"}])),
    ]
    tool = cache_iv.Tools()

    result = run(
        tool.cache_tool_call(
            "execute_sql",
            __messages__=messages,
            __request__=request,
            __user__={"id": "user"},
            __metadata__={"chat_id": "chat", "session_id": "session"},
        )
    )

    assert set(result) == {"status", "cache_id", "source_tool"}
    assert result["status"] == "ok"
    entry = request.state.cached_tool_calls[0]
    assert entry["tool_call_id"] == "call-new"
    assert entry["arguments"] == {"sql": "new"}
    assert entry["result"] == [{"v": "new"}]


def test_cache_tool_call_reports_missing_call_and_result():
    tool = cache_iv.Tools()
    missing_call = run(tool.cache_tool_call("execute_sql", __messages__=[]))
    assert missing_call["error"] == "Tool call not found"

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-no-result",
                    "function": {"name": "execute_sql", "arguments": "{}"},
                }
            ],
        }
    ]
    missing_result = run(tool.cache_tool_call("execute_sql", __messages__=messages))
    assert missing_result["error"] == "Matching tool result not found"


def test_find_latest_tool_call_in_open_webui_output():
    output = response_output(
        ("call-old", "execute_sql", {"sql": "old"}, [{"v": "old"}]),
        ("call-other", "other_tool", {}, [{"v": "other"}]),
        ("call-new", "execute_sql", {"sql": "new"}, [{"v": "new"}]),
    )

    call_data, error = cache_iv._find_latest_output_tool_call(output, "execute_sql")

    assert error is None
    assert call_data == {
        "tool_id": "execute_sql",
        "tool_call_id": "call-new",
        "arguments": {"sql": "new"},
        "result": [{"v": "new"}],
    }


def test_cache_tool_call_prefers_chat_message_output(monkeypatch):
    async def load_chat_message_output(request, metadata):
        assert metadata == {"chat_id": "chat", "message_id": "assistant-message"}
        return response_output(
            ("call-live", "execute_sql", {"sql": "live"}, [{"v": "live"}])
        )

    monkeypatch.setattr(
        cache_iv, "_load_chat_message_output", load_chat_message_output
    )
    stale_messages = history(
        ("call-stale", "execute_sql", {"sql": "stale"}, [{"v": "stale"}])
    )
    request = DummyRequest()

    result = run(
        cache_iv.Tools().cache_tool_call(
            "execute_sql",
            __messages__=stale_messages,
            __request__=request,
            __metadata__={"chat_id": "chat", "message_id": "assistant-message"},
        )
    )

    assert result["status"] == "ok"
    assert request.state.cached_tool_calls[0]["tool_call_id"] == "call-live"
    assert request.state.cached_tool_calls[0]["result"] == [{"v": "live"}]


def test_load_chat_message_output_uses_ids_and_prefers_active_stream(monkeypatch):
    saved_output = response_output(
        ("call-saved", "execute_sql", {"sql": "saved"}, [{"v": "saved"}])
    )
    live_output = response_output(
        ("call-live", "execute_sql", {"sql": "live"}, [{"v": "live"}])
    )
    calls = []

    class FakeChats:
        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            calls.append(("message", chat_id, message_id))
            return {"output": saved_output}

    async def get_response_streams_by_chat_id(redis, chat_id):
        calls.append(("stream", redis, chat_id))
        return [
            {
                "chat_id": chat_id,
                "message_id": "assistant-message",
                "output": live_output,
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
    output = run(
        cache_iv._load_chat_message_output(
            request,
            {"chat_id": "chat", "message_id": "assistant-message"},
        )
    )

    assert output == live_output
    assert calls == [
        ("message", "chat", "assistant-message"),
        ("stream", "redis-connection", "chat"),
    ]


def test_separate_tools_share_the_same_request_cache():
    request = DummyRequest()
    cached = run(
        cache_iv.Tools().cache_tool_call(
            "execute_sql",
            __messages__=history(
                ("call-shared", "execute_sql", {"sql": "x"}, [{"shared": True}])
            ),
            __request__=request,
        )
    )

    _, html = capture_visualize(
        iv.Tools(), cache_id=cached["cache_id"], __request__=request
    )

    assert extract_cached_json(html) == [{"shared": True}]


def test_legacy_visualize_and_pinned_libraries():
    tool = iv.Tools()
    result, plotly_html = capture_visualize(tool, title="Legacy")
    assert "waiting for content" in result
    assert iv._LIBRARY_URLS["plotly"] in plotly_html
    assert "data-iv-build=\"2.3.0\"" in plotly_html

    _, chart_html = capture_visualize(tool, title="Chart", library="chartjs")
    assert iv._LIBRARY_URLS["chartjs"] in chart_html
    assert iv._LIBRARY_URLS["plotly"] not in chart_html


@pytest.mark.parametrize("library", ["d3", "echarts"])
def test_unsupported_library_is_controlled_error(library):
    result = run(iv.Tools().visualize(library=library))
    assert result == {
        "status": "error",
        "error": "Unsupported visualization library",
        "library": library,
        "supported_libraries": ["chartjs", "plotly"],
    }


def test_invalid_cache_id_is_controlled_error():
    result = run(
        iv.Tools().visualize(
            cache_id="missing",
            __request__=DummyRequest(),
            __user__={"id": "user"},
            __metadata__={"chat_id": "chat", "session_id": "session"},
        )
    )
    assert result["status"] == "error"
    assert result["error"] == "Cache entry not found"
    assert "Re-run the query" in result["message"]


def test_visualize_injects_only_selected_cache_without_arguments():
    request = DummyRequest()
    request.state.cached_tool_calls = [
        cache_entry("cache-a", [{"marker": "DO_NOT_INCLUDE_A"}]),
        cache_entry("cache-b", [{"marker": "ONLY_SELECTED_B"}]),
        cache_entry("cache-c", [{"marker": "DO_NOT_INCLUDE_C"}]),
    ]

    _, html = capture_visualize(
        iv.Tools(), cache_id="cache-b", __request__=request, library="plotly"
    )

    assert extract_cached_json(html) == [{"marker": "ONLY_SELECTED_B"}]
    assert "DO_NOT_INCLUDE_A" not in html
    assert "DO_NOT_INCLUDE_C" not in html
    assert "SELECT secret" not in html
    assert "getCachedData" in html
    assert '"sourceTool":"execute_sql"' in html


def test_two_cached_datasets_render_independently():
    request = DummyRequest()
    request.state.cached_tool_calls = [
        cache_entry("cache-a", [{"series": "A"}]),
        cache_entry("cache-b", [{"series": "B"}]),
    ]

    _, html_a = capture_visualize(
        iv.Tools(), cache_id="cache-a", __request__=request
    )
    _, html_b = capture_visualize(
        iv.Tools(), cache_id="cache-b", __request__=request
    )

    assert extract_cached_json(html_a) == [{"series": "A"}]
    assert extract_cached_json(html_b) == [{"series": "B"}]


@pytest.mark.parametrize(
    "payload",
    [
        [{"kind": "list"}],
        {"kind": "dict"},
        "plain string",
        "Русский текст",
        None,
    ],
)
def test_cached_bridge_round_trip_for_supported_result_shapes(payload):
    bridge, _ = iv._build_cached_data_bridge(cache_entry(result=payload))
    assert extract_cached_json(bridge) == payload


def test_safe_json_round_trip_blocks_script_breakout_and_preserves_unicode():
    payload = {
        "ru": "Привет",
        "attack": "</script><script>window.pwned=true</script>",
        "symbols": "<>&",
        "separators": "before\u2028middle\u2029after",
    }
    bridge, _ = iv._build_cached_data_bridge(cache_entry(result=payload))

    assert "</script><script>window.pwned" not in bridge
    assert "\\u003c/script\\u003e" in bridge
    assert "\\u0026" in bridge
    assert "\u2028" not in bridge
    assert "\u2029" not in bridge
    assert extract_cached_json(bridge) == payload


def test_oversized_dataset_returns_controlled_error_without_embed():
    request = DummyRequest()
    request.state.cached_tool_calls = [cache_entry(result="123456")]
    tool = iv.Tools()
    tool.valves.max_cached_data_bytes = 4

    result, html = capture_visualize(tool, cache_id="cache-b", __request__=request)

    assert html is None
    assert result["error"] == "Cached dataset is too large for inline visualization"
    assert result["size_bytes"] > result["limit_bytes"]


def test_process_fallback_is_scoped_by_user_chat_and_session():
    cache_tool = cache_iv.Tools()
    visualizer = iv.Tools()
    first_request = DummyRequest()
    messages = history(
        ("call-fallback", "execute_sql", {"sql": "x"}, [{"fallback": True}])
    )
    user = {"id": "user"}
    metadata = {"chat_id": "chat", "session_id": "session"}
    cached = run(
        cache_tool.cache_tool_call(
            "execute_sql",
            __messages__=messages,
            __request__=first_request,
            __user__=user,
            __metadata__=metadata,
        )
    )

    _, html = capture_visualize(
        visualizer,
        cache_id=cached["cache_id"],
        __request__=DummyRequest(app=first_request.app),
        __user__=user,
        __metadata__=metadata,
    )
    assert extract_cached_json(html) == [{"fallback": True}]

    wrong_scope = run(
        visualizer.visualize(
            cache_id=cached["cache_id"],
            __request__=DummyRequest(app=first_request.app),
            __user__={"id": "other"},
            __metadata__=metadata,
        )
    )
    assert wrong_scope["error"] == "Cache entry not found"


def test_runtime_order_csp_and_external_script_guards():
    entry = cache_entry(result=[{"x": 1}])
    bridge, _ = iv._build_cached_data_bridge(entry)
    html = iv._build_html(
        security_level="strict",
        library="chartjs",
        cached_data_bridge=bridge,
    )

    assert html.index("function sendPrompt") < html.index('id="iv-cached-data"')
    assert html.index("getCachedData") < html.index('data-iv-library="chartjs"')
    assert html.index('data-iv-library="chartjs"') < html.index("var START_MARK")
    assert iv._LIBRARY_URLS["chartjs"] in html
    assert iv._LIBRARY_URLS["plotly"] not in html
    assert "cdnjs.cloudflare.com" not in html
    assert "unpkg.com" not in html
    assert "'unsafe-eval'" not in html
    assert "if (src)" in html
    assert "_ivIsBlockedScript" in html
    assert "External scripts are not allowed" in html


def test_offline_uses_fixed_self_hosted_library_path():
    html = iv._build_html(security_level="offline", library="plotly")
    assert iv._OFFLINE_LIBRARY_URLS["plotly"] in html
    assert iv._LIBRARY_URLS["plotly"] not in html
    assert "script-src 'unsafe-inline' 'self'" in html
