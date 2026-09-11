---
name: tool-call-cache
description: Cache the latest completed native tool result for reuse by Inline Visualizer within the same user request. Use when a data-producing tool result should be passed by cache_id instead of copied into another tool call. Do not use as persistent or cross-request storage.
---

# Tool Call Cache

Use `cache_tool_call()` to turn a completed native tool result into a compact `cache_id`. This skill covers cache orchestration only. Load and follow the `visualize` skill separately when rendering the cached data.

## Required workflow

1. Call the data-producing tool and wait for its result.
2. Immediately call `cache_tool_call(tool_id="<exact function name>")`.
3. Check that the cache response has `status: "ok"` and retain the returned `cache_id` exactly.
4. In the same user request and sequential tool loop, pass that exact ID to the consumer:

```text
execute_sql(...)
→ wait for result
→ cache_tool_call(tool_id="execute_sql")
→ {"status":"ok","cache_id":"…","source_tool":"execute_sql"}
→ visualize(title="…", cache_id="…")
```

Never call the producer and `cache_tool_call()` in parallel. The cache tool resolves the latest completed invocation whose function name exactly matches `tool_id` and binds it to its result by tool-call ID.

## Rules

- Pass only the producer's exact function name as `tool_id`; do not pass its display name, tool-call ID, arguments, SQL, or result.
- Do not copy the producer result into `cache_tool_call()`, `visualize()`, generated HTML, or generated JavaScript.
- Never invent, modify, or reuse a `cache_id` from an earlier user request.
- Do not provide reserved arguments such as `__messages__`, `__request__`, or `__metadata__`; Open WebUI supplies them internally.
- If the same producer tool must run more than once, cache each result immediately after that invocation and before invoking the producer again.
- Each `visualize()` call accepts one `cache_id`. If one visualization needs several datasets, have the producer return one combined dataset, then cache that single result.

## Lifetime and failures

The cache is request-local. A `cache_id` is valid only during the same user request and assistant tool loop in which it was created. It does not survive a later user message, a new request, or a process restart.

If `cache_tool_call()` returns `status: "error"`, do not call `visualize()` with a missing or guessed ID:

- For `Tool call not found` or `Matching tool result not found`, verify the exact producer function name and that the producer completed before the cache call.
- For `Cache storage unavailable`, report that the result could not be cached in the current request.
- If `visualize()` reports `Cache entry not found`, repeat producer → cache → visualize within one request only when rerunning the producer is safe and idempotent; otherwise report the failure instead of repeating a side effect.
