import asyncio
import importlib.util
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tool.py"
SPEC = importlib.util.spec_from_file_location("inline_visualizer_v2_tool", MODULE_PATH)
iv = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(iv)


def run(awaitable):
    return asyncio.run(awaitable)


def test_visualize_requires_source_tool_call_id():
    parameters = inspect.signature(iv.Tools.visualize).parameters

    assert parameters["source_tool_call_id"].default is inspect.Parameter.empty
    assert "title" in parameters
    assert "__messages__" not in parameters
    assert "__request__" not in parameters
    with pytest.raises(ValueError, match="non-empty"):
        run(iv.Tools().visualize(source_tool_call_id=" "))


def test_visualize_mounts_streaming_wrapper_with_escaped_call_id():
    events = []

    async def emitter(event):
        events.append(event)

    result = run(
        iv.Tools().visualize(
            source_tool_call_id='call_1"><script>alert(1)',
            title="SQL chart",
            __event_emitter__=emitter,
        )
    )

    assert "getToolData()" in result
    assert "@@@VIZ-START" in result
    assert len(events) == 1
    html = events[0]["data"]["embeds"][0]
    assert 'data-iv-build="2.3.0"' in html
    assert 'data-iv-source-tool-call-id="call_1&quot;&gt;&lt;script&gt;alert(1)"' in html
    assert "function getToolData()" in html
    assert "parent.fetch(url" in html
    assert "connect-src 'none'" in html


def test_visualize_without_event_emitter_returns_html_response_tuple():
    response, context = run(
        iv.Tools().visualize(source_tool_call_id="call_sql", title="Fallback")
    )

    assert isinstance(response, iv.HTMLResponse)
    assert response.headers["content-disposition"] == "inline"
    assert "getToolData()" in context
    assert b'iv-source-tool-call-id="call_sql"' in response.body


def test_strict_mode_preserves_original_csp_and_runtime():
    html = iv._build_html(security_level="strict")

    assert "cdnjs.cloudflare.com" in html
    assert "cdn.jsdelivr.net" in html
    assert "unpkg.com" in html
    assert "'unsafe-eval'" in html
    assert "connect-src 'none'" in html
    assert "if (src)" in html


def test_offline_mode_preserves_original_self_hosted_script_policy():
    html = iv._build_html(security_level="offline")

    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self'" in html


def execute_tool_data_helper(chat, call_id="call_sql", path="/c/chat-1", invoke=True):
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the browser helper test")

    helper = iv.BODY_SCRIPTS.split("// --- Height reporting ---", 1)[0]
    helper = helper.removeprefix("\n<script>\n")
    script = f"""
const vm = require('vm');
const helper = {json.dumps(helper)};
const chat = {json.dumps(chat)};
const callId = {json.dumps(call_id)};
const path = {json.dumps(path)};
const invoke = {json.dumps(invoke)};
let fetchCount = 0;
const notices = [];
const area = {{ parentNode: {{ insertBefore: (notice) => notices.push(notice) }} }};
const context = {{
  document: {{
    documentElement: {{ getAttribute: () => callId }},
    getElementById: (id) => id === 'iv-render' ? area : notices.find((n) => n.id === id),
    createElement: () => ({{ id: '', style: {{}}, setAttribute: () => {{}}, textContent: '' }})
  }},
  parent: {{
    location: {{ pathname: path }},
    localStorage: {{ getItem: () => 'token' }},
    fetch: async (url, options) => {{
      fetchCount++;
      if (url !== '/api/v1/chats/chat-1' ||
          options.headers.Authorization !== 'Bearer token') throw Error('bad request');
      return {{ ok: true, json: async () => chat }};
    }}
  }},
  setTimeout,
  clearTimeout
}};
vm.runInNewContext(helper, context);
if (invoke) {{
  context.getToolData().then(async (result) => {{
    if (result && result.rows) result.rows[0].y = 999;
    const second = await context.getToolData();
    process.stdout.write(JSON.stringify({{ result, second, fetchCount, notices }}));
  }}, (error) => process.stdout.write(JSON.stringify({{ error: error.message, notices }})));
}} else {{
  setTimeout(() => process.stdout.write(JSON.stringify({{ fetchCount, notices }})), 20);
}}
"""
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=5
    )
    return json.loads(result.stdout)


def test_get_tool_data_finds_json_in_current_or_earlier_message():
    chat = {
        "chat": {
            "history": {
                "messages": {
                    "old-message": {
                        "output": [
                            {
                                "type": "function_call_output",
                                "call_id": "call_other",
                                "output": [{"type": "input_text", "text": "[]"}],
                            }
                        ]
                    },
                    "current-message": {
                        "output": [
                            {
                                "type": "function_call_output",
                                "call_id": "call_sql",
                                "output": [
                                    {
                                        "type": "input_text",
                                        "text": '{"rows":[{"x":1,"y":2},{"x":2,"y":3}]}',
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        }
    }

    result = execute_tool_data_helper(chat)

    assert result["result"]["rows"][0]["y"] == 999
    assert result["second"] == {"rows": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]}
    assert result["fetchCount"] == 1
    assert execute_tool_data_helper(chat, call_id="call_other")["result"] == []


def test_get_tool_data_rejects_ambiguous_or_invalid_result():
    item = {
        "type": "function_call_output",
        "call_id": "call_sql",
        "output": [{"type": "input_text", "text": "not json"}],
    }
    chat = {"chat": {"history": {"messages": {"a": {"output": [item]}}}}}
    assert "JSON" in execute_tool_data_helper(chat)["error"]

    chat["chat"]["history"]["messages"]["b"] = {"output": [item]}
    assert "ambiguous" in execute_tool_data_helper(chat)["error"]


def test_source_is_validated_even_when_generated_html_never_calls_helper():
    chat = {"chat": {"history": {"messages": {"a": {"output": [
        {"type": "function_call_output", "call_id": "call_sql", "output": "not json"}
    ]}}}}}

    result = execute_tool_data_helper(chat, invoke=False)

    assert result["fetchCount"] == 1
    assert len(result["notices"]) == 1
    assert "Tool data unavailable" in result["notices"][0]["textContent"]


def test_get_tool_data_requires_saved_chat():
    result = execute_tool_data_helper({}, path="/")
    assert "saved chat" in result["error"]
