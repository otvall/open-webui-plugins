import asyncio
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


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
    assert match is not None
    return json.loads(match.group(1))


def capture_visualize(tool, **kwargs):
    events = []

    async def emitter(event):
        events.append(event)

    result = run(tool.visualize_tool_result(__event_emitter__=emitter, **kwargs))
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
    parameter = inspect.signature(iv.Tools.visualize_tool_result).parameters[
        "source_tool_call_id"
    ]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


def test_visualize_without_event_emitter_returns_html_response_tuple():
    response, context = run(
        iv.Tools().visualize_tool_result(
            source_tool_call_id="call-data",
            title="Fallback",
            __messages__=[tool_message("call-data", [{"x": 1}])],
        )
    )

    assert isinstance(response, iv.HTMLResponse)
    assert response.headers["content-disposition"] == "inline"
    assert "waiting for content" in context
    assert b"getToolData" in response.body
    assert b'tool-result-1.0.0' in response.body


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


def test_unknown_or_unfinished_source_tool_call_id_is_a_controlled_error(
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

    assert result["status"] == "error"
    assert result["error"] == "Tool result not found"
    assert result["source_tool_call_id"] == "call-pending"
    assert "later tool round" in result["message"]
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
    assert result["error"] == "Tool result cannot be serialized"
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
    assert "cdnjs.cloudflare.com" in html
    assert "cdn.jsdelivr.net" in html
    assert "unpkg.com" in html
    assert "'unsafe-eval'" in html
    assert "if (src)" in html
    assert "_ivIsBlockedScript" not in html
    assert "External scripts are not allowed" not in html


def test_offline_preserves_original_self_hosted_script_policy():
    html = iv._build_html(security_level="offline")
    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self'" in html
