import asyncio
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import types
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tool.py"
SPEC = importlib.util.spec_from_file_location("inline_visualizer_v2_tool", MODULE_PATH)
iv = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(iv)


def run(awaitable):
    return asyncio.run(awaitable)


def test_visualize_contract_and_early_invalid_id():
    parameters = inspect.signature(iv.Tools.visualize).parameters
    assert "source_tool_call_id" in parameters
    assert "title" in parameters
    assert "html_code" not in parameters
    assert "__messages__" in parameters
    result = run(iv.Tools().visualize(source_tool_call_id=" "))
    assert result["status"] == "error"
    assert "source_tool_call_id" in result["message"]


def test_completed_fragment_is_in_embed_after_tool_data_helper():
    fragment = '<div id="chart"></div><script>getToolData().then(draw)</script>'
    document = iv._build_html(
        fragment,
        source_tool_call_id='call_1"><script>alert(1)',
        visualization_id="a" * 32,
    )
    assert 'data-iv-build="3.0.0"' in document
    assert 'data-iv-source-tool-call-id="call_1&quot;&gt;&lt;script&gt;alert(1)"' in document
    assert iv._slot_id(document) == "a" * 32
    assert document.index("function getToolData()") < document.index(fragment)
    assert document.index(fragment) < document.index("requestAnimationFrame(_ivReady)")
    assert "@@@VIZ-START" not in document
    assert "parent.fetch(url" in document
    assert "connect-src 'none'" in document


def test_progress_and_slot_replacement_keep_other_embeds():
    slot = "a" * 32
    progress = iv._build_progress(slot, "Sales", 123456789)
    completed = iv._build_html("<div>ready</div>", visualization_id=slot)
    assert iv._slot_id(progress) == slot
    assert "sessionStorage.setItem" in progress
    assert "Visualization generation timed out" in progress
    result = iv._upsert_slot(["another embed", progress], slot, completed)
    assert result == ["another embed", completed]
    error = iv._build_progress(slot, "Sales", 123456789, "Failed")
    assert "sessionStorage.removeItem" in error
    assert 'role="alert"' in error


def test_visualize_validates_source_then_replaces_placeholder(monkeypatch):
    published = []
    captured = {}

    async def resolve(call_id, request, metadata, messages):
        captured["source"] = call_id
        return True, {"rows": [{"x": 1, "y": 2}]}, "functions.call_sql"

    async def publish(request, metadata, user, emitter, slot, document):
        published.append((slot, document))

    async def generate(request, user, model_id, messages, max_tokens):
        captured.update(model_id=model_id, messages=messages)
        return '<div id="chart">Chart</div><script>getToolData().then(draw)</script>'

    monkeypatch.setattr(iv, "_resolve_tool_result_with_id", resolve)
    monkeypatch.setattr(iv, "_publish_slot", publish)
    monkeypatch.setattr(iv, "_generate_fragment", generate)
    request = SimpleNamespace(state=SimpleNamespace(metadata={}))
    metadata = {"chat_id": "chat-1", "message_id": "message-1", "params": {"function_calling": "native"}}
    messages = [{"role": "system", "content": "Use Russian"},
                {"role": "user", "content": "Plot x against y"}]
    result = run(iv.Tools().visualize(
        source_tool_call_id="call_sql", title="Sales",
        __messages__=messages, __request__=request, __metadata__=metadata,
        __user__={"id": "user-1"}, __model__={"id": "main-model"},
        __event_emitter__=lambda _: None,
    ))
    assert result["status"] == "success"
    assert captured["source"] == "call_sql"
    assert captured["model_id"] == "main-model"
    assert "Plot x against y" in str(captured["messages"])
    assert "Use Russian" in str(captured["messages"])
    assert '"x": 1' in str(captured["messages"])
    assert len(published) == 2
    assert published[0][0] == published[1][0]
    assert iv._slot_id(published[0][1]) == iv._slot_id(published[1][1])
    assert "Rendering visualization" in published[0][1]
    assert "getToolData().then(draw)" in published[1][1]
    assert 'data-iv-source-tool-call-id="functions.call_sql"' in published[1][1]


def test_invalid_source_does_not_start_generation(monkeypatch):
    async def resolve(*args):
        return False, None, "missing"
    monkeypatch.setattr(iv, "_resolve_tool_result_with_id", resolve)
    request = SimpleNamespace(state=SimpleNamespace(metadata={}))
    result = run(iv.Tools().visualize(
        source_tool_call_id="missing", __messages__=[], __request__=request,
        __metadata__={"chat_id": "c", "message_id": "m", "params": {"function_calling": "native"}},
        __user__={"id": "u"}, __model__={"id": "main"}, __event_emitter__=lambda _: None,
    ))
    assert result["status"] == "error"
    assert "not found" in result["message"]


def test_generation_failure_replaces_loading_with_error(monkeypatch):
    documents = []
    async def resolve(*args):
        return True, {"rows": []}, "call_sql"
    async def publish(request, metadata, user, emitter, slot, document):
        documents.append(document)
    async def generate(*args):
        raise ValueError("incomplete response")
    monkeypatch.setattr(iv, "_resolve_tool_result_with_id", resolve)
    monkeypatch.setattr(iv, "_publish_slot", publish)
    monkeypatch.setattr(iv, "_generate_fragment", generate)
    request = SimpleNamespace(state=SimpleNamespace(metadata={}))
    result = run(iv.Tools().visualize(
        source_tool_call_id="call_sql", __messages__=[{"role": "user", "content": "Chart"}],
        __request__=request,
        __metadata__={"chat_id": "c", "message_id": "m", "params": {"function_calling": "native"}},
        __user__={"id": "u"}, __model__={"id": "main"}, __event_emitter__=lambda _: None,
    ))
    assert result["status"] == "error"
    assert len(documents) == 2
    assert "Rendering visualization" in documents[0]
    assert "Visualization generation failed" in documents[1]
    assert iv._slot_id(documents[0]) == iv._slot_id(documents[1])


def test_full_context_over_limit_fails_without_calling_model(monkeypatch):
    documents = []

    async def resolve(*args):
        return True, {"rows": []}, "call_sql"

    async def publish(request, metadata, user, emitter, slot, document):
        documents.append(document)

    async def generate(*args):
        pytest.fail("the model must not receive a shortened conversation")

    monkeypatch.setattr(iv, "_resolve_tool_result_with_id", resolve)
    monkeypatch.setattr(iv, "_publish_slot", publish)
    monkeypatch.setattr(iv, "_generate_fragment", generate)
    tool = iv.Tools()
    tool.valves.generation_context_max_chars = 10000
    result = run(tool.visualize(
        source_tool_call_id="call_sql",
        __messages__=[{"role": "user", "content": "A" * 11000}],
        __request__=SimpleNamespace(state=SimpleNamespace(metadata={})),
        __metadata__={"chat_id": "c", "message_id": "m", "params": {"function_calling": "native"}},
        __user__={"id": "u"}, __model__={"id": "main"}, __event_emitter__=lambda _: None,
    ))
    assert result["status"] == "error"
    assert "no messages were removed" in result["message"]
    assert len(documents) == 2


def test_csp_variants():
    strict = iv._build_html("<div>ok</div>", security_level="strict")
    offline = iv._build_html("<div>ok</div>", security_level="offline")
    assert "cdnjs.cloudflare.com" in strict
    assert "'unsafe-eval'" in strict
    assert "connect-src 'none'" in strict
    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self'" in offline


def test_source_resolution_uses_exact_call_id_and_current_output():
    messages = [{"output": [
        {"type": "function_call_output", "call_id": "functions.call_sql",
         "output": [{"type": "input_text", "text": '{"rows":[{"x":1}]}'}]},
        {"type": "function_call_output", "call_id": "functions.other",
         "output": [{"type": "input_text", "text": '{"rows":[]}'}]},
    ]}]
    found, data, resolved = run(iv._resolve_tool_result_with_id(
        "call_sql", None, {}, messages,
    ))
    assert found is True
    assert resolved == "functions.call_sql"
    assert data == {"rows": [{"x": 1}]}
    found, _, _ = run(iv._resolve_tool_result_with_id("unknown", None, {}, messages))
    assert found is False


def test_publish_slot_replaces_only_its_own_embed(monkeypatch):
    chat = {"embeds": ["existing embed"]}

    class Chats:
        @staticmethod
        async def get_chat_by_id_and_user_id(chat_id, user_id):
            return {"id": chat_id}

        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            return chat.copy()

    module = types.ModuleType("open_webui.models.chats")
    module.Chats = Chats
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", module)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    metadata = {"chat_id": "c", "message_id": "m"}

    async def emit(event):
        assert event["type"] == "embeds"
        assert event["data"]["replace"] is True
        chat["embeds"] = event["data"]["embeds"]

    slot = "a" * 32
    pending = iv._build_progress(slot, "Chart", 123456789)
    complete = iv._build_html("<div>chart</div>", visualization_id=slot)
    run(iv._publish_slot(request, metadata, {"id": "u"}, emit, slot, pending))
    run(iv._publish_slot(request, metadata, {"id": "u"}, emit, slot, complete))
    assert chat["embeds"] == ["existing embed", complete]


def test_internal_generation_is_nonstreaming(monkeypatch):
    calls = []

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            return SimpleNamespace(id=user_id, role="user")

    async def completion(request, form_data, **kwargs):
        calls.append((form_data, kwargs))
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": "<div>chart</div>"}}]}

    async def resolve_model(request, user, model_id):
        return model_id

    users_module = types.ModuleType("open_webui.models.users")
    users_module.Users = Users
    chat_module = types.ModuleType("open_webui.utils.chat")
    chat_module.generate_chat_completion = completion
    monkeypatch.setitem(sys.modules, "open_webui.models.users", users_module)
    monkeypatch.setitem(sys.modules, "open_webui.utils.chat", chat_module)
    monkeypatch.setattr(iv, "_resolve_generation_model", resolve_model)
    request = SimpleNamespace(scope={"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    result = run(iv._generate_fragment(
        request, {"id": "u"}, "main", [{"role": "user", "content": "Chart sales"}], 12000,
    ))
    assert result == "<div>chart</div>"
    assert calls[0][0]["stream"] is False
    assert calls[0][0]["model"] == "main"
    assert calls[0][1]["bypass_system_prompt"] is True


def test_generated_iframe_scripts_parse_in_node():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the JavaScript syntax check")

    class ScriptCollector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inside = False
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.inside = True
                self.scripts.append("")

        def handle_endtag(self, tag):
            if tag == "script":
                self.inside = False

        def handle_data(self, data):
            if self.inside:
                self.scripts[-1] += data

    for document in (
        iv._build_progress("a" * 32, "Chart", 123456789),
        iv._build_html("<div>Chart</div>", visualization_id="a" * 32),
    ):
        parser = ScriptCollector()
        parser.feed(document)
        for script in parser.scripts:
            subprocess.run(["node", "--check"], input=script, text=True,
                           capture_output=True, check=True, timeout=5)


def execute_tool_data_helper(chat, call_id="call_sql", path="/c/chat-1", invoke=True):
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the browser helper test")

    helper = "// Load one JSON tool result" + iv.BODY_SCRIPTS.split("// Load one JSON tool result", 1)[1].split("// --- Height reporting ---", 1)[0]
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
  _ivFail: () => {{}},
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
