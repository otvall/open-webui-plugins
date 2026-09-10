"""
title: Tool Call Cache for Inline Visualizer
author: Classic298
author_url: https://github.com/Classic298
version: 2.3.0
required_open_webui_version: 0.10.2
description: Caches the latest completed native tool result and returns a compact cache_id for Inline Visualizer.
"""

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Optional
from uuid import uuid4


_APP_CACHE_ATTR = "_inline_visualizer_tool_call_cache_v1"
_PROCESS_CACHE_TTL_SECONDS = 30 * 60
_PROCESS_CACHE_MAX_ENTRIES_PER_SCOPE = 8
_PROCESS_CACHE_MAX_BYTES = 128 * 1024 * 1024


def _normalize_cached_result(result: Any) -> Any:
    if isinstance(result, (list, dict)) or result is None:
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result


def _normalize_tool_arguments(arguments: Any) -> dict[str, Any] | str:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else arguments
        except (json.JSONDecodeError, TypeError):
            return arguments
    return str(arguments)


def _tool_message_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    text_parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("text", "input_text", "output_text"):
            text_parts.append(str(part.get("text", "")))
    return "".join(text_parts)


def _find_latest_tool_call(
    messages: list[dict[str, Any]], tool_id: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Find the newest named call and its later result by tool_call_id."""
    for call_message_index in range(len(messages) - 1, -1, -1):
        message = messages[call_message_index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tool_call in reversed(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            if not isinstance(function, dict) or function.get("name") != tool_id:
                continue
            tool_call_id = str(tool_call.get("id") or "")
            if not tool_call_id:
                return None, "Tool call has no tool_call_id"
            for result_message in messages[call_message_index + 1 :]:
                if (
                    isinstance(result_message, dict)
                    and result_message.get("role") == "tool"
                    and str(result_message.get("tool_call_id") or "")
                    == tool_call_id
                ):
                    return (
                        {
                            "tool_id": tool_id,
                            "tool_call_id": tool_call_id,
                            "arguments": _normalize_tool_arguments(
                                function.get("arguments", {})
                            ),
                            "result": _normalize_cached_result(
                                _tool_message_content(result_message.get("content"))
                            ),
                        },
                        None,
                    )
            return None, "Matching tool result not found"
    return None, "Tool call not found"


def _output_item_content(output: Any) -> Any:
    """Extract text from an Open WebUI function_call_output payload."""
    if not isinstance(output, list):
        return output
    text_parts = []
    for part in output:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("text", "input_text", "output_text"):
            text_parts.append(str(part.get("text", "")))
    return "".join(text_parts)


def _find_latest_output_tool_call(
    output: list[dict[str, Any]], tool_id: str
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Find a completed call in Open WebUI's persisted assistant output."""
    for call_index in range(len(output) - 1, -1, -1):
        item = output[call_index]
        if (
            not isinstance(item, dict)
            or item.get("type") != "function_call"
            or item.get("name") != tool_id
        ):
            continue
        tool_call_id = str(item.get("call_id") or item.get("id") or "")
        if not tool_call_id:
            return None, "Tool call has no tool_call_id"
        for result_item in output[call_index + 1 :]:
            if (
                isinstance(result_item, dict)
                and result_item.get("type") == "function_call_output"
                and str(result_item.get("call_id") or "") == tool_call_id
            ):
                return (
                    {
                        "tool_id": tool_id,
                        "tool_call_id": tool_call_id,
                        "arguments": _normalize_tool_arguments(
                            item.get("arguments", {})
                        ),
                        "result": _normalize_cached_result(
                            _output_item_content(result_item.get("output"))
                        ),
                    },
                    None,
                )
        return None, "Matching tool result not found"
    return None, "Tool call not found"


async def _load_chat_message_output(
    __request__, __metadata__
) -> Optional[list[dict[str, Any]]]:
    """Load the current assistant output by chat_id/message_id.

    Open WebUI 0.11.x keeps native tool-loop items in the assistant message's
    ``output`` field instead of updating the reserved ``__messages__`` value.
    An active response stream is newer than the database row, so it wins when
    both are available.
    """
    metadata = __metadata__ if isinstance(__metadata__, dict) else {}
    chat_id = str(metadata.get("chat_id") or "")
    message_id = str(
        metadata.get("message_id") or metadata.get("assistant_message_id") or ""
    )
    if not (chat_id and message_id):
        return None

    stored_output = None
    try:
        from open_webui.models.chats import Chats

        message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
        if isinstance(message, dict) and isinstance(message.get("output"), list):
            stored_output = message["output"]
    except Exception:
        pass

    try:
        from open_webui.tasks import get_response_streams_by_chat_id

        app = getattr(__request__, "app", None) if __request__ is not None else None
        app_state = getattr(app, "state", None) if app is not None else None
        redis = getattr(app_state, "redis", None) if app_state is not None else None
        streams = await get_response_streams_by_chat_id(redis, chat_id)
        for stream in reversed(streams or []):
            if (
                isinstance(stream, dict)
                and str(stream.get("message_id") or "") == message_id
                and isinstance(stream.get("output"), list)
            ):
                return stream["output"]
    except Exception:
        pass

    return stored_output


def _get_request_cache(__request__, create: bool = False) -> Optional[list[dict]]:
    state = getattr(__request__, "state", None) if __request__ is not None else None
    if state is None:
        return None
    cached = getattr(state, "cached_tool_calls", None)
    if cached is None and create:
        cached = []
        setattr(state, "cached_tool_calls", cached)
    return cached if isinstance(cached, list) else None


def _cache_scope_key(__user__, __metadata__) -> Optional[tuple[str, str, str]]:
    user = __user__ if isinstance(__user__, dict) else {}
    metadata = __metadata__ if isinstance(__metadata__, dict) else {}
    user_id = str(user.get("id") or metadata.get("user_id") or "")
    chat_id = str(metadata.get("chat_id") or "")
    session_id = str(metadata.get("session_id") or "")
    if not (user_id and chat_id and session_id):
        return None
    return user_id, chat_id, session_id


def _serialized_size(value: Any) -> int:
    try:
        serialized = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        serialized = json.dumps(str(value), ensure_ascii=False)
    return len(serialized.encode("utf-8"))


def _get_app_cache(__request__, create: bool = False) -> Optional[dict[str, Any]]:
    app = getattr(__request__, "app", None) if __request__ is not None else None
    app_state = getattr(app, "state", None) if app is not None else None
    if app_state is None:
        return None
    cache = getattr(app_state, _APP_CACHE_ATTR, None)
    if cache is None and create:
        cache = {"buckets": OrderedDict(), "lock": asyncio.Lock()}
        setattr(app_state, _APP_CACHE_ATTR, cache)
    return cache if isinstance(cache, dict) else None


async def _store_process_cache(
    __request__,
    scope: Optional[tuple[str, str, str]],
    entry: dict[str, Any],
) -> bool:
    cache = _get_app_cache(__request__, create=True)
    if scope is None or cache is None:
        return False
    now = time.monotonic()
    entry_size = _serialized_size(entry.get("result"))
    async with cache["lock"]:
        buckets = cache["buckets"]
        expired = [
            key
            for key, bucket in buckets.items()
            if now - bucket["updated_at"] > _PROCESS_CACHE_TTL_SECONDS
        ]
        for key in expired:
            buckets.pop(key, None)

        bucket = buckets.pop(
            scope, {"updated_at": now, "entries": [], "size_bytes": 0}
        )
        bucket["entries"].append((entry, entry_size))
        bucket["size_bytes"] += entry_size
        while len(bucket["entries"]) > _PROCESS_CACHE_MAX_ENTRIES_PER_SCOPE:
            _, removed_size = bucket["entries"].pop(0)
            bucket["size_bytes"] -= removed_size
        bucket["updated_at"] = now
        buckets[scope] = bucket

        total_size = sum(item["size_bytes"] for item in buckets.values())
        while total_size > _PROCESS_CACHE_MAX_BYTES and buckets:
            _, removed = buckets.popitem(last=False)
            total_size -= removed["size_bytes"]
    return True


class Tools:
    """Cache completed native tool results for Inline Visualizer."""

    async def cache_tool_call(
        self,
        tool_id: str,
        __messages__=None,
        __request__=None,
        __user__=None,
        __metadata__=None,
    ):
        """Cache the latest completed invocation of ``tool_id``.

        Call this only after the data-producing tool result has returned. The
        current assistant message is resolved from chat_id/message_id, then its
        native tool-loop output is matched by tool_call_id. Reserved __messages__
        is used only as a compatibility fallback. The result is never included
        in this tool's response.

        :param tool_id: Exact function.name of the data-producing tool.
        :return: A compact cache_id reference for visualize().
        """
        output = await _load_chat_message_output(__request__, __metadata__)
        call_data, output_error = (
            _find_latest_output_tool_call(output, tool_id)
            if isinstance(output, list)
            else (None, None)
        )
        messages = __messages__ if isinstance(__messages__, list) else []
        messages_error = None
        if call_data is None:
            call_data, messages_error = _find_latest_tool_call(messages, tool_id)
        if call_data is None:
            return {
                "status": "error",
                "error": (
                    output_error
                    if output_error and output_error != "Tool call not found"
                    else messages_error or output_error or "Tool call not found"
                ),
                "tool_id": tool_id,
            }

        entry = {
            "cache_id": str(uuid4()),
            "tool_id": call_data["tool_id"],
            "tool_call_id": call_data["tool_call_id"],
            "arguments": call_data["arguments"],
            "result": call_data["result"],
        }
        request_cache = _get_request_cache(__request__, create=True)
        if request_cache is not None:
            request_cache.append(entry)

        fallback_stored = await _store_process_cache(
            __request__, _cache_scope_key(__user__, __metadata__), entry
        )
        if request_cache is None and not fallback_stored:
            return {
                "status": "error",
                "error": "Cache storage unavailable",
                "tool_id": tool_id,
            }
        return {
            "status": "ok",
            "cache_id": entry["cache_id"],
            "source_tool": entry["tool_id"],
        }
