import asyncio
import copy
import html
import importlib.util
import inspect
import json
import os
import re
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


def test_generation_omits_whitespace_only_history_messages():
    messages = iv._generation_messages(
        [
            {"role": "user", "content": "Plot the result"},
            {"role": "assistant", "content": "\n \n", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "\n\n\n"}]},
            ]},
            {"role": "assistant", "content": "\n", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Data is ready"}]},
            ]},
        ],
        {},
        {"rows": [{"day": "2026-09-01", "online": 12}]},
        "Sales",
    )
    assert all(str(message["content"]).strip() for message in messages)
    assert {message["content"] for message in messages if message["role"] == "assistant"} == {"Data is ready"}


@pytest.fixture
def owui(monkeypatch):
    """OWUI boundaries only: run the complete public tool without an LLM/server."""
    from starlette.requests import Request

    user = SimpleNamespace(id="user-1", role="user")
    metadata = {
        "chat_id": "chat-1", "message_id": "message-1",
        "model_id": "chat-model", "params": {"function_calling": "native"},
    }
    app = SimpleNamespace(state=SimpleNamespace(
        redis=None, MODELS={"chat-model": {"id": "chat-model"}},
    ))
    env = SimpleNamespace(
        metadata=metadata,
        request=Request({"type": "http", "app": app, "state": {"metadata": metadata}}),
        messages=[{"role": "user", "content": "Plot daily revenue using the visualize skill"}],
        output=[], saved_message={"embeds": [], "output": []},
        generations=[], events=[],
        session_auth=False, provider_authorization=None,
        injected_model={"id": "chat-model"},
        fragment='<div id="chart">Revenue chart</div>',
    )

    class Chats:
        @staticmethod
        async def get_chat_by_id_and_user_id(chat_id, user_id):
            assert (chat_id, user_id) == ("chat-1", "user-1")
            return {"id": chat_id}

        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            assert (chat_id, message_id) == ("chat-1", "message-1")
            return copy.deepcopy(env.saved_message)

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            assert user_id == "user-1"
            return user

    class Models:
        @staticmethod
        async def get_model_by_id(model_id):
            return None

    async def get_all_models(*args, **kwargs):
        return list(app.state.MODELS.values())

    async def check_model_access(user, model):
        return None

    async def get_response_streams_by_chat_id(redis, chat_id):
        assert chat_id == "chat-1"
        return [{"message_id": "message-1", "output": copy.deepcopy(env.output)}] if env.output else []

    async def generate_chat_completion(request, form_data, **kwargs):
        if env.session_auth:
            # Same requirement as OWUI's OpenAI connection auth_type="session".
            env.provider_authorization = f"Bearer {request.state.token.credentials}"
        env.generations.append((request, copy.deepcopy(form_data)))
        return {"choices": [{"finish_reason": "stop", "message": {"content": env.fragment}}]}

    async def emit(event):
        env.events.append(copy.deepcopy(event))
        assert event["type"] == "embeds"
        if event["data"].get("replace"):
            env.saved_message["embeds"] = copy.deepcopy(event["data"]["embeds"])
        else:
            env.saved_message["embeds"].extend(event["data"]["embeds"])

    boundaries = {
        "open_webui.models.chats": {"Chats": Chats},
        "open_webui.models.users": {"Users": Users},
        "open_webui.models.models": {"Models": Models},
        "open_webui.utils.models": {"get_all_models": get_all_models, "check_model_access": check_model_access},
        "open_webui.utils.chat": {"generate_chat_completion": generate_chat_completion},
        "open_webui.tasks": {"get_response_streams_by_chat_id": get_response_streams_by_chat_id},
    }
    for name, attributes in boundaries.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)

    env.tool = iv.Tools()
    env.invoke = lambda **kwargs: env.tool.visualize(
        source_tool_call_id="call_sql", title="Daily revenue",
        __request__=env.request, __metadata__=env.metadata,
        __user__={"id": user.id}, __model__=env.injected_model,
        __messages__=env.messages, __event_emitter__=emit, **kwargs,
    )
    return env


@pytest.mark.parametrize("source", ["live", "saved"])
def test_visualize_generates_with_the_skill_and_source_query_from_this_answer(owui, source):
    skill = (MODULE_PATH.parent / "SKILL.md").read_text()
    query = "SELECT day, SUM(revenue) AS revenue FROM sales GROUP BY day"
    output = [
        {"type": "function_call", "call_id": "call_sql", "name": "query", "arguments": query},
        {"type": "function_call_output", "call_id": "call_sql", "status": "completed",
         "output": [{"type": "input_text", "text": '{"rows":[{"day":"Monday","revenue":42}]}'}]},
        {"type": "function_call", "call_id": "call_skill", "name": "view_skill", "arguments": '{"name":"visualize"}'},
        {"type": "function_call_output", "call_id": "call_skill", "status": "completed",
         "output": [{"type": "input_text", "text": skill}]},
        {"type": "function_call", "call_id": "call_visualize", "name": "visualize", "arguments": '{"source_tool_call_id":"call_sql"}'},
        {"type": "function_call_output", "call_id": "call_visualize", "status": "in_progress", "output": []},
    ]
    if source == "live":
        owui.output = output
    else:
        owui.saved_message["output"] = output

    result = run(owui.invoke())

    assert result["status"] == "success"
    context = json.dumps(owui.generations[0][1]["messages"], ensure_ascii=False)
    assert "Build ambitiously when the topic supports it" in context
    assert query in context
    assert "call_visualize" not in context
    assert "Revenue chart" in owui.events[-1]["data"]["embeds"][0]


@pytest.mark.parametrize("override,expected_model", [("", "chat-model"), ("custom-model", "custom-model")])
def test_visualize_uses_the_chat_model_unless_explicitly_overridden(owui, override, expected_model):
    # OWUI 0.11.1 injects the configured task model as __model__ for local tools.
    owui.injected_model = {"id": "task-model"}
    owui.request.app.state.MODELS.update({
        "task-model": {"id": "task-model"}, "custom-model": {"id": "custom-model"},
    })
    owui.tool.valves.generation_model_id = override
    owui.messages.append({"role": "tool", "tool_call_id": "call_sql", "content": '{"rows":[]} '})

    result = run(owui.invoke())

    assert result["status"] == "success"
    assert owui.generations[0][1]["model"] == expected_model


def test_visualize_can_generate_through_a_session_authenticated_connection(owui):
    owui.session_auth = True
    owui.request.state.token = SimpleNamespace(credentials="test-session-token")
    owui.messages.append({"role": "tool", "tool_call_id": "call_sql", "content": '{"rows":[]}'})

    result = run(owui.invoke())

    assert result["status"] == "success"
    assert owui.provider_authorization == "Bearer test-session-token"
    # Session credentials must not bring the outer chat/tool execution into the child.
    assert owui.generations[0][0].state.metadata == {"iv_generation": True}
    assert "Revenue chart" in owui.events[-1]["data"]["embeds"][0]


@pytest.fixture
def chromium():
    """Use an installed Chromium; no downloads or browser dependencies in pytest."""
    configured = os.environ.get("IV_TEST_CHROMIUM")
    if configured:
        assert Path(configured).is_file(), "IV_TEST_CHROMIUM must point to a Chromium executable"
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell"):
        executable = shutil.which(name)
        if executable:
            return executable
    for cache in (Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"):
        for name in ("chrome-headless-shell", "headless_shell", "chrome"):
            for executable in sorted(cache.glob(f"chromium*/**/{name}"), reverse=True):
                if executable.is_file() and os.access(executable, os.X_OK):
                    return str(executable)
    pytest.skip("Install Chromium or set IV_TEST_CHROMIUM to run the iframe browser regression")


@pytest.mark.parametrize("handler,expected_error", [
    ("throw new Error('Chart update failed')", "Chart update failed"),
    ("Promise.reject(new Error('Chart update failed'))", "Chart update failed"),
    ("throw new Error('Load failed')", "Load failed"),
    ("_ivIsIOS = true; HTMLAnchorElement.prototype.click = function() { "
     "window.dispatchEvent(new ErrorEvent('error', {message: 'Load failed', cancelable: true})); "
     "}; _ivDownload();", None),
    ("_ivIsIOS = true; HTMLAnchorElement.prototype.click = function() { "
     "Promise.reject(new Error('Load failed')); }; _ivDownload();", None),
    ("_ivIsIOS = true; HTMLAnchorElement.prototype.click = function() { "
     "window.dispatchEvent(new ErrorEvent('error', {message: 'Chart update failed', cancelable: true})); "
     "}; _ivDownload();", "Chart update failed"),
], ids=["script-error", "unhandled-rejection", "load-error-outside-download",
        "ios-download-error", "ios-download-rejection", "chart-error-during-download"])
def test_visualization_handles_errors_after_the_chart_is_ready(owui, chromium, tmp_path, handler, expected_error):
    owui.output = [{
        "type": "function_call_output", "call_id": "call_sql", "status": "completed",
        "output": [{"type": "input_text", "text": '{"rows":[]}'}],
    }]
    owui.fragment = f'<button id="update-chart" onclick="{html.escape(handler, quote=True)}">Update chart</button>'
    result = run(owui.invoke())
    assert result["status"] == "success"
    document = owui.events[-1]["data"]["embeds"][0]
    # Chromium's --virtual-time-budget advances timers but not compositor frames.
    # Replace only the animation clock, before any iframe scripts execute.
    clock = """<script>
window.requestAnimationFrame = function(callback) { return setTimeout(function() { callback(performance.now()); }, 16); };
window.cancelAnimationFrame = clearTimeout;
</script>"""
    document = document.replace("<head>", "<head>" + clock, 1)
    chat = {"chat": {"history": {"messages": {"message-1": {"output": owui.output}}}}}
    # Replace the external saved chat API; the complete iframe still runs with
    # actual DOM, CSS and JavaScript event handling in Chromium.
    page = """<!doctype html><html><body><pre id="test-result">pending</pre>
<script>
window.fetch = async function() { return {ok: true, json: async function() { return CHAT; }}; };
function report(value) { document.getElementById('test-result').textContent = JSON.stringify(value); }
function inspectChart() {
  var frame = document.getElementById('visualization');
  var doc = frame.contentDocument, win = frame.contentWindow;
  var button = doc && doc.getElementById('update-chart');
  if (!button || win.getComputedStyle(button).visibility !== 'visible' || doc.getElementById('iv-loader')) {
    setTimeout(inspectChart, 50);
    return;
  }
  button.click();
  setTimeout(function() {
    var alerts = Array.from(doc.querySelectorAll('[role="alert"]')).filter(function(el) {
      return win.getComputedStyle(el).visibility === 'visible' && el.getClientRects().length > 0;
    }).map(function(el) { return el.textContent; });
    report({readyBeforeClick: true, alerts: alerts,
      chartVisible: win.getComputedStyle(button).visibility === 'visible'});
  }, 100);
}
setTimeout(inspectChart, 50);
</script>
<iframe id="visualization" sandbox="allow-scripts allow-same-origin" srcdoc="DOCUMENT"></iframe>
</body></html>""".replace("CHAT", json.dumps(chat)).replace("DOCUMENT", html.escape(document, quote=True))
    path = tmp_path / "c" / "chat-1" / "index.html"
    path.parent.mkdir(parents=True)
    path.write_text(page)
    completed = subprocess.run([
        chromium, "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
        "--allow-file-access-from-files", f"--user-data-dir={tmp_path / 'browser-profile'}",
        "--dump-dom", "--virtual-time-budget=5000", path.as_uri(),
    ], text=True, capture_output=True, timeout=30, check=True)
    match = re.search(r'<pre id="test-result">([^<]*)</pre>', completed.stdout)
    assert match and match[1] != "pending", completed.stderr[-3000:]
    observed = json.loads(html.unescape(match[1]))
    assert observed["readyBeforeClick"] is True
    if expected_error:
        assert any(expected_error in alert for alert in observed["alerts"]), observed
        assert observed["chartVisible"] is False
    else:
        assert observed["alerts"] == [], observed
        assert observed["chartVisible"] is True


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
