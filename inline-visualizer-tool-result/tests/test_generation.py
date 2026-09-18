"""Public short-call workflow; mocked OWUI persistence/provider, no paid requests."""
import asyncio
import copy
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.requests import Request

spec = importlib.util.spec_from_file_location("iv_generation", Path(__file__).parents[1] / "tool.py")
iv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iv)

FRAGMENT = '<div id="value"></div><script>document.getElementById("value").textContent=getToolData().value;</script>'


def artifact(document):
    return json.loads(re.search(r'<script id="iv-visualization-artifact" type="application/json">(.*?)</script>', document, re.S)[1])


@pytest.fixture
def host(monkeypatch):
    state = SimpleNamespace(MODELS={"main": {"id": "main"}, "fast": {"id": "fast"}})
    request = Request({"type": "http", "app": SimpleNamespace(state=state),
                       "state": {"metadata": {"tools": {"secret": 1}, "chat_id": "c"}, "bypass_filter": "original"}})
    saved = {"embeds": ["https://example.org/existing"]}
    events, calls = [], []
    h = SimpleNamespace(request=request, saved=saved, events=events, calls=calls,
                        access=True, model_error=None, result=None, started=None, release=None,
                        model_records={}, model_checks=[], denied_models=set(), refreshed_models=None, refresh_count=0,
                        pipe_handler=None)

    class Models:
        @staticmethod
        async def get_model_by_id(model_id):
            info = h.model_records.get(model_id)
            if info is None:
                return None
            return SimpleNamespace(base_model_id=info.get("base_model_id"), model_dump=lambda: copy.deepcopy(info))

    async def check_model_access(user, model):
        h.model_checks.append(model["id"])
        if model["id"] in h.denied_models:
            raise ValueError("Access denied")

    async def get_all_models(request, refresh=False, user=None):
        assert refresh is True
        h.refresh_count += 1
        if h.refreshed_models is not None:
            request.app.state.MODELS = copy.deepcopy(h.refreshed_models)
        return list(request.app.state.MODELS.values())

    class Chats:
        @staticmethod
        async def get_chat_by_id_and_user_id(chat_id, user_id):
            assert chat_id == "c" and user_id == "u"
            return {"id": "c"} if h.access else None

        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            assert (chat_id, message_id) == ("c", "m")
            await asyncio.sleep(0)
            return copy.deepcopy(saved)

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            assert user_id == "u"
            return SimpleNamespace(id="u", role="user")

    async def completion(child, body, **kwargs):
        await check_model_access(kwargs["user"], {"id": body["model"]})
        calls.append((child, copy.deepcopy(body), kwargs))
        assert events and "Загрузка" in events[0]["data"]["embeds"][-1]
        if h.started:
            h.started.set()
        if h.release:
            await h.release.wait()
        if h.model_error:
            raise h.model_error
        if h.pipe_handler is not None and child.app.state.MODELS[body["model"]].get("pipe"):
            return await h.pipe_handler(child, body)
        return h.result or {"choices": [{"finish_reason": "stop", "message": {"content": FRAGMENT}}]}

    async def emit(event):
        await asyncio.sleep(0)
        events.append(copy.deepcopy(event))
        assert event["data"]["replace"] is True
        saved["embeds"] = copy.deepcopy(event["data"]["embeds"])

    for name, attributes in {
        "open_webui.models.chats": {"Chats": Chats},
        "open_webui.models.users": {"Users": Users},
        "open_webui.models.models": {"Models": Models},
        "open_webui.utils.models": {"check_model_access": check_model_access, "get_all_models": get_all_models},
        "open_webui.utils.chat": {"generate_chat_completion": completion},
        "open_webui.utils.chat_id": {"is_saved_chat_id": lambda chat_id: chat_id == "c"},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    h.kwargs = dict(source_tool_call_id="source", instruction="Plot value", title="Value",
                    __request__=request, __user__={"id": "u", "oauth": "never forward"},
                    __metadata__={"chat_id": "c", "message_id": "m", "params": {"function_calling": "native"}},
                    __model__={"id": "main"}, __event_emitter__=emit,
                    __messages__=[{"role": "system", "content": "Use Russian"},
                                  {"role": "user", "content": "Build a chart"},
                                  {"role": "tool", "tool_call_id": "source", "content": '{"value":42}'}])
    return h


def test_short_public_contract():
    params = inspect.signature(iv.Tools.visualize_tool_result).parameters
    assert "html" not in params
    assert params["instruction"].default is inspect.Parameter.empty
    assert params["source_tool_call_id"].default is inspect.Parameter.empty


def test_public_tool_repairs_missing_functions_prefix_and_saves_canonical_id(host):
    host.kwargs["source_tool_call_id"] = "get_sales:1"
    host.kwargs["__messages__"][-1]["tool_call_id"] = "functions.get_sales:1"
    response, _ = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    saved = artifact(response.body.decode())
    assert saved["sourceToolCallId"] == "functions.get_sales:1"
    assert saved["data"] == {"value": 42}
    assert len(host.calls) == 1


@pytest.mark.parametrize("location", ["live", "saved", "message", "nested", "direct"])
def test_missing_prefix_resolves_across_output_formats(monkeypatch, location):
    output = {"type": "function_call_output", "call_id": "functions.sales:1", "output": '{"value":42}', "status": "completed"}
    outputs, messages = [], []
    if location == "live":
        outputs = [[output]]
    elif location == "saved":
        outputs = [[], [output]]
    elif location == "message":
        messages = [{"role": "tool", "tool_call_id": output["call_id"], "content": output["output"]}]
    elif location == "nested":
        messages = [{"role": "assistant", "output": [output]}]
    else:
        messages = [output]
    async def load(*args):
        return outputs
    monkeypatch.setattr(iv, "_load_current_message_outputs", load)
    assert asyncio.run(iv._resolve_tool_result_with_id("sales:1", None, None, messages)) == (
        True, {"value": 42}, "functions.sales:1")


def test_exact_history_id_wins_over_prefixed_live_id(monkeypatch):
    async def load(*args):
        return [[{"type": "function_call_output", "call_id": "functions.sales:1", "output": "wrong"}]]
    monkeypatch.setattr(iv, "_load_current_message_outputs", load)
    messages = [{"role": "tool", "tool_call_id": "sales:1", "content": "exact"}]
    assert asyncio.run(iv._resolve_tool_result_with_id("sales:1", None, None, messages)) == (True, "exact", "sales:1")


@pytest.mark.parametrize("pending", [
    {"type": "function_call", "call_id": "sales:1", "status": "in_progress"},
    {"type": "function_call_output", "call_id": "sales:1", "status": "pending", "output": "not ready"},
    {"role": "assistant", "tool_calls": [{"id": "sales:1"}]},
])
def test_existing_unfinished_exact_call_blocks_prefix_fallback(pending):
    messages = [pending, {"role": "tool", "tool_call_id": "functions.sales:1", "content": "wrong call"}]
    assert asyncio.run(iv._resolve_tool_result_with_id("sales:1", None, None, messages)) == (False, None, "sales:1")


@pytest.mark.parametrize("submitted", ["sales", "sales:2", "function.sales:1", "other.sales:1", "functions.functions.sales:1", "Sales:1"])
def test_only_missing_literal_prefix_is_repaired(submitted):
    messages = [{"role": "tool", "tool_call_id": "functions.sales:1", "content": "data"}]
    assert asyncio.run(iv._resolve_tool_result_with_id(submitted, None, None, messages)) == (False, None, submitted)


def test_prefixed_pending_result_is_not_accepted():
    messages = [{"type": "function_call_output", "call_id": "functions.sales:1", "status": "pending", "output": "partial"}]
    assert asyncio.run(iv._resolve_tool_result_with_id("sales:1", None, None, messages)) == (False, None, "sales:1")


def test_placeholder_precedes_generation_and_final_is_durable(host):
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert len(host.events) == 2 and len(host.calls) == 1
    pending = host.events[0]["data"]["embeds"][-1]
    final = response.body.decode()
    assert host.saved["embeds"] == ["https://example.org/existing", final]
    assert iv._slot_id(pending) == iv._slot_id(final) == artifact(final)["visualizationId"]
    assert artifact(final)["data"] == {"value": 42}
    assert artifact(final)["html"] == FRAGMENT
    assert "iv-visualization-artifact" not in pending and "/static/" not in pending
    assert "Загрузка" in pending and "self-contained" in context
    assert not host.request.app.state.iv_embed_locks


def test_request_isolation_context_and_model_override(host):
    tool = iv.Tools()
    tool.valves.generation_model_id = "fast"
    original = copy.deepcopy(host.request.scope["state"])
    asyncio.run(tool.visualize_tool_result(**host.kwargs))
    child, body, options = host.calls[0]
    assert child is not host.request and child.scope["state"] is not host.request.scope["state"]
    assert host.request.scope["state"] == original
    assert child.state.metadata == {"iv_generation": True}
    assert body["model"] == "fast" and body["stream"] is False
    assert body["max_tokens"] == 8000 and "tools" not in body and "tool_choice" not in body
    assert options["bypass_filter"] is False
    assert options["bypass_system_prompt"] is True
    assert "Use Russian" in body["messages"][0]["content"]
    assert "Build a chart" in json.dumps(body)
    assert "never forward" not in json.dumps(body)
    assert all(m["role"] != "tool" and "tool_calls" not in m for m in body["messages"])


@pytest.mark.parametrize("source", ["cache", "database"])
def test_workspace_agent_uses_physical_kimi_without_preset_configuration(host, source):
    info = {"id": "main", "base_model_id": "Kimi_K2.6", "is_active": True,
            "params": {"system": "PRESET_SYSTEM", "temperature": 0.9},
            "meta": {"toolIds": ["private-tool"], "skillIds": ["private-skill"]}}
    host.request.app.state.MODELS["Kimi_K2.6"] = {"id": "Kimi_K2.6", "owned_by": "openai"}
    if source == "database":
        del host.request.app.state.MODELS["main"]
        host.model_records["main"] = info
    else:
        host.request.app.state.MODELS["main"] = {"id": "main", "preset": True, "info": info}
    original_state = copy.deepcopy(host.request.scope["state"])
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert artifact(response.body.decode())["data"] == {"value": 42}
    child, body, options = host.calls[0]
    assert body["model"] == "Kimi_K2.6"
    assert host.model_checks == ["main", "Kimi_K2.6"]
    assert options["bypass_filter"] is False
    assert "PRESET_SYSTEM" not in json.dumps(body) and "private-tool" not in json.dumps(body)
    assert "private-skill" not in json.dumps(body) and "temperature" not in body
    assert "Build a chart" in json.dumps(body)
    assert host.request.scope["state"] == original_state


def test_nested_workspace_alias_and_explicit_override(host):
    host.request.app.state.MODELS.update({
        "override": {"id": "override", "info": {"base_model_id": "middle"}},
        "middle": {"id": "middle", "info": {"base_model_id": "fast"}},
    })
    tool = iv.Tools()
    tool.valves.generation_model_id = "override"
    asyncio.run(tool.visualize_tool_result(**host.kwargs))
    assert host.calls[0][1]["model"] == "fast"
    assert host.model_checks == ["override", "middle", "fast"]


@pytest.mark.parametrize("workspace", [True, False])
def test_logging_pipe_is_called_instead_of_bypassed(host, workspace):
    pipe_id = "logging.Kimi_K2.6"
    host.request.app.state.MODELS[pipe_id] = {"id": pipe_id, "pipe": {"type": "pipe"}}
    # Also make a direct model available: it must NOT be selected by guessing.
    host.request.app.state.MODELS["Kimi_K2.6"] = {"id": "Kimi_K2.6"}
    if workspace:
        host.request.app.state.MODELS["main"] = {"id": "main", "preset": True, "info": {"base_model_id": pipe_id}}
    else:
        host.kwargs["__model__"] = {"id": pipe_id}
    logged = []
    async def logging_pipe(child, body):
        logged.append(body["model"])
        assert body["model"] == pipe_id and body["stream"] is False
        assert "tools" not in body and "tool_ids" not in body and "skill_ids" not in body
        assert child.state.metadata == {"iv_generation": True}
        assert not any(key in child.state.metadata for key in ("chat_id", "session_id", "message_id", "tools"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": FRAGMENT}}]}
    host.pipe_handler = logging_pipe
    response, _ = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert logged == [pipe_id]
    assert artifact(response.body.decode())["data"] == {"value": 42}
    assert host.model_checks == (["main", pipe_id] if workspace else [pipe_id])
    assert not iv._IV_GENERATION_ACTIVE.get()


def test_pipe_cannot_reenter_visualizer_even_if_it_drops_metadata(host):
    host.request.app.state.MODELS["main"]["pipe"] = True
    async def recursive_pipe(child, body):
        # Simulate an adapter losing the metadata guard but staying in the same
        # async request context. It must not start a second generation/embed.
        nested = await iv.Tools().visualize_tool_result(**host.kwargs)
        assert nested["status"] == "error" and "Recursive" in nested["message"]
        with pytest.raises(iv._GenerationModelError, match="Повторный вход"):
            await iv._generate_fragment(child, {"id": "u"}, "main", [], 1000)
        return {"choices": [{"finish_reason": "stop", "message": {"content": FRAGMENT}}]}
    host.pipe_handler = recursive_pipe
    asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert len(host.calls) == 1 and len(host.events) == 2


def test_pipe_failure_releases_recursion_guard_for_next_request(host):
    host.request.app.state.MODELS["main"]["pipe"] = True
    async def scenario():
        async def broken_pipe(child, body):
            raise RuntimeError("logging adapter unavailable")
        host.pipe_handler = broken_pipe
        _, context = await iv.Tools().visualize_tool_result(**host.kwargs)
        assert "failed" in context and not iv._IV_GENERATION_ACTIVE.get()
        host.pipe_handler = None
        response, _ = await iv.Tools().visualize_tool_result(**host.kwargs)
        assert artifact(response.body.decode())["data"] == {"value": 42}
        assert not iv._IV_GENERATION_ACTIVE.get()
    asyncio.run(scenario())


def test_missing_base_cache_refreshes_once(host):
    host.request.app.state.MODELS["main"] = {"id": "main", "info": {"base_model_id": "Kimi_K2.6"}}
    host.refreshed_models = {"Kimi_K2.6": {"id": "Kimi_K2.6"}}
    asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert host.refresh_count == 1 and host.calls[0][1]["model"] == "Kimi_K2.6"


@pytest.mark.parametrize("denied", ["main", "fast"])
def test_unwrapping_does_not_bypass_preset_or_base_permissions(host, denied):
    host.request.app.state.MODELS["main"] = {"id": "main", "info": {"base_model_id": "fast"}}
    host.denied_models.add(denied)
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert "failed" in context and not host.calls
    assert 'role="alert"' in response.body.decode()


@pytest.mark.parametrize("kind, expected", [
    ("cycle", "циклическая"), ("missing", "не найдена"), ("disabled", "отключена"),
    ("arena", "является Arena"), ("direct", "напрямую из браузера"),
])
def test_bad_workspace_routes_have_specific_visible_errors(host, kind, expected):
    host.request.app.state.MODELS["main"] = {"id": "main", "info": {"base_model_id": "fast"}}
    if kind == "cycle":
        host.request.app.state.MODELS["fast"]["info"] = {"base_model_id": "main"}
    elif kind == "missing":
        del host.request.app.state.MODELS["fast"]
    elif kind == "disabled":
        host.request.app.state.MODELS["main"]["info"]["is_active"] = False
    elif kind == "arena":
        host.request.app.state.MODELS["fast"]["owned_by"] = "arena"
    else:
        host.request.app.state.MODELS["fast"]["connection_type"] = "direct"
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert expected in response.body.decode() and expected in context
    assert not host.calls


def test_context_normalizes_completed_outputs_and_drops_dangling_protocol():
    messages = [{"role": "assistant", "content": "decision", "tool_calls": [{"id": "unfinished"}],
                 "reasoning_content": "private", "embeds": ["huge HTML"],
                 "output": [{"type": "function_call_output", "call_id": "old", "output": "retained"}]},
                {"type": "function_call_output", "call_id": "pending", "output": "ignore", "status": "pending"}]
    original = copy.deepcopy(messages)
    result = iv._generation_messages(messages, {"sources": [{"document": "reference"}]}, None, "task", "title")
    serialized = json.dumps(result)
    assert "retained" in serialized and "reference" in serialized
    assert "unfinished" not in serialized and "private" not in serialized and "huge HTML" not in serialized
    assert "ignore" not in serialized and messages == original


def test_completed_call_arguments_and_responses_text_remain_context():
    messages = [{"role": "assistant", "tool_calls": [{"id": "done", "function": {"name": "sql", "arguments": "units=millions"}}]},
                {"role": "tool", "tool_call_id": "done", "content": "42"},
                {"role": "assistant", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Prior explanation"}]}]}]
    result = iv._generation_messages(messages, {}, 42, "task", "title")
    assert "units=millions" in json.dumps(result) and "Prior explanation" in json.dumps(result)
    assert all("tool_calls" not in m for m in result)


def test_unpersisted_event_does_not_start_paid_request(host):
    async def no_save(event):
        pass
    result = asyncio.run(iv.Tools().visualize_tool_result(**{**host.kwargs, "__event_emitter__": no_save}))
    assert result["status"] == "error" and not host.calls


@pytest.mark.parametrize("response", [
    {"choices": [{"finish_reason": "length", "message": {"content": FRAGMENT}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": FRAGMENT, "tool_calls": [{"id": "x"}]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": FRAGMENT, "refusal": "no"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "```html\n<div>x</div>\n```"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "no HTML"}}]},
    {"error": "provider failure"},
    {"choices": [{"finish_reason": "stop", "message": {"content": "<div><script>unfinished"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "<html><body>wrong wrapper</body></html>"}}]},
])
def test_failed_responses_replace_loading_without_retry(host, response):
    host.result = response
    result, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert len(host.calls) == 1 and len(host.saved["embeds"]) == 2
    final = result.body.decode()
    assert 'role="alert"' in final and "Не удалось" in final
    assert "generation failed" in context
    assert "iv-visualization-artifact" not in final


def test_timeout_and_cancellation(host):
    async def scenario():
        host.started, host.release = asyncio.Event(), asyncio.Event()
        tool = iv.Tools()
        tool.valves.generation_timeout_seconds = 0.02
        response, context = await tool.visualize_tool_result(**host.kwargs)
        assert 'role="alert"' in response.body.decode() and "failed" in context
        tool.valves.generation_timeout_seconds = 120
        host.started.clear()
        task = asyncio.create_task(tool.visualize_tool_result(**host.kwargs))
        await host.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "Генерация прервана" in host.saved["embeds"][-1]
        assert not host.request.app.state.iv_embed_locks
    asyncio.run(scenario())


def test_simultaneous_instances_preserve_all_slots(host):
    async def scenario():
        results = await asyncio.gather(*(iv.Tools().visualize_tool_result(**{**host.kwargs, "title": str(i)}) for i in range(5)))
        assert len(host.saved["embeds"]) == 6
        assert host.saved["embeds"][0] == "https://example.org/existing"
        ids = {artifact(response.body.decode())["visualizationId"] for response, _ in results}
        assert {iv._slot_id(doc) for doc in host.saved["embeds"][1:]} == ids
        assert all("iv-visualization-artifact" in doc for doc in host.saved["embeds"][1:])
        assert not host.request.app.state.iv_embed_locks
    asyncio.run(scenario())


def test_slot_merge_preserves_order_and_collapses_only_matching_duplicates():
    key, other = "a" * 32, "b" * 32
    old = iv._build_progress(key, "a", time.time())
    neighbor = iv._build_progress(other, "b", time.time())
    assert iv._upsert_slot(["untouched", old, neighbor, old], key, "final") == ["untouched", "final", neighbor]


def test_access_failure_never_calls_model_or_overwrites(host):
    host.access = False
    result = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert result["status"] == "error" and not host.calls and not host.events
    assert host.saved["embeds"] == ["https://example.org/existing"]


@pytest.mark.parametrize("kind", ["arena", "missing"])
def test_unsupported_model_fails_visibly_without_provider_call(host, kind):
    if kind == "missing":
        host.request.app.state.MODELS.clear()
    else:
        host.request.app.state.MODELS["main"] = {"owned_by": "arena"}
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert "failed" in context and 'role="alert"' in response.body.decode() and not host.calls


def test_oversize_context_is_not_silently_truncated(host):
    tool = iv.Tools()
    tool.valves.generation_context_max_chars = 1000
    response, context = asyncio.run(tool.visualize_tool_result(**host.kwargs))
    assert "failed" in context and not host.calls and 'role="alert"' in response.body.decode()


def test_missing_source_retry_carries_instruction_not_html(host):
    args = {**host.kwargs, "source_tool_call_id": "missing"}
    result = asyncio.run(iv.Tools().visualize_tool_result(**args))
    assert result["status"] == "retry_required"
    retry = result["retry"]["arguments"]
    assert retry["instruction"] == "Plot value" and "html" not in retry
    result = asyncio.run(iv.Tools().visualize_tool_result(**{**args, **retry}))
    assert result["status"] == "error" and not host.events and not host.calls


def test_final_save_error_does_not_claim_success_or_regenerate(host):
    original = host.kwargs["__event_emitter__"]
    async def emit(event):
        if host.events:
            raise RuntimeError("DB unavailable")
        await original(event)
    response, context = asyncio.run(iv.Tools().visualize_tool_result(**{**host.kwargs, "__event_emitter__": emit}))
    assert artifact(response.body.decode())["data"] == {"value": 42}
    assert "save failed" in context and len(host.calls) == 1


def test_redis_failure_is_fail_closed(host):
    class BrokenRedis:
        def lock(self, *args, **kwargs):
            raise RuntimeError("Redis unavailable")
    host.request.app.state.redis = BrokenRedis()
    result = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert result["status"] == "error" and not host.calls and not host.events
    assert not host.request.app.state.iv_embed_locks


def test_redis_lock_wraps_read_emit_and_releases(host):
    acquired = []
    class Redis:
        def lock(self, key, **kwargs):
            assert key.startswith("iv:embed-update:") and kwargs == {"timeout": 30, "blocking_timeout": 5}
            class Lease:
                async def __aenter__(self):
                    acquired.append(True)
                async def __aexit__(self, *args):
                    acquired.append(False)
            return Lease()
    host.request.app.state.redis = Redis()
    asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    assert acquired == [True, False, True, False]


@pytest.mark.skipif(not os.environ.get("IV_BROWSER_TESTS"), reason="Set IV_BROWSER_TESTS=1")
def test_browser_loading_replace_reload_and_expiry(host):
    response, _ = asyncio.run(iv.Tools().visualize_tool_result(**host.kwargs))
    final = response.body.decode()
    key = iv._slot_id(final)
    fixtures = {
        "pending": host.events[0]["data"]["embeds"][-1], "final": final,
        "expired": iv._build_progress(key, "Value", time.time() - 1),
        "error": iv._build_progress(key, "<img src=x onerror=alert(1)>", 0, "Генерация прервана."),
        "key": key,
    }
    completed = subprocess.run(
        [shutil.which("node") or "node", str(Path(__file__).with_name("browser_generation.cjs"))],
        input=json.dumps(fixtures), text=True, capture_output=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1])["restored"] is True
