import asyncio
import importlib.util
import inspect
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tool.py"
SPEC = importlib.util.spec_from_file_location("inline_visualizer_v2_tool", MODULE_PATH)
iv = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(iv)


def run(awaitable):
    return asyncio.run(awaitable)


def test_visualize_exposes_only_standard_arguments():
    parameters = inspect.signature(iv.Tools.visualize).parameters

    assert "title" in parameters
    assert "tool_call_id" not in parameters
    assert "source_tool_call_id" not in parameters
    assert "__messages__" not in parameters
    assert "__request__" not in parameters
    assert "__metadata__" not in parameters


def test_visualize_mounts_original_streaming_wrapper():
    events = []

    async def emitter(event):
        events.append(event)

    result = run(
        iv.Tools().visualize(
            title="Standard visualization",
            __event_emitter__=emitter,
        )
    )

    assert "waiting for content" in result
    assert "@@@VIZ-START" in result
    assert len(events) == 1
    html = events[0]["data"]["embeds"][0]
    assert 'data-iv-build="2.2.2"' in html
    assert "getToolData" not in html
    assert 'id="iv-tool-data"' not in html


def test_visualize_without_event_emitter_returns_html_response_tuple():
    response, context = run(iv.Tools().visualize(title="Fallback"))

    assert isinstance(response, iv.HTMLResponse)
    assert response.headers["content-disposition"] == "inline"
    assert "waiting for content" in context
    assert b"getToolData" not in response.body


def test_strict_mode_preserves_original_csp_and_runtime():
    html = iv._build_html(security_level="strict")

    assert "cdnjs.cloudflare.com" in html
    assert "cdn.jsdelivr.net" in html
    assert "unpkg.com" in html
    assert "'unsafe-eval'" in html
    assert "if (src)" in html
    assert "_ivIsBlockedScript" not in html
    assert "External scripts are not allowed" not in html


def test_offline_mode_preserves_original_self_hosted_script_policy():
    html = iv._build_html(security_level="offline")

    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self'" in html
