# Inline Visualizer — Tool Result

Version 1.4 displays a **loading placeholder before a separate model generates HTML**,
then replaces that slot with completed HTML and a snapshot of one tool result.
The final embed is self-contained: restoration does not need
VIZ markers, assistant-message DOM, a new producer call, or an old browser cache.

## Requirements

- Open WebUI 0.10.2 or newer; validate upgrades against your deployed OWUI version.
- Install both `tool.py` and the `visualize-tool-result` skill.
- Use native function calling in a saved chat. The tool requires authenticated
  user/request context and the server's event emitter; temporary chats are rejected.
- Internal generation uses `generation_model_id` (empty: current model). Select
  a server-connected model, not a Pipe, Arena or browser-direct connection.
- Serve Chart.js at `/static/chart.umd.min.js` and Plotly at
  `/static/plotly.umd.min.js`. These are the only supported libraries.
  The tool does not install them. Override `chartjs_url` / `plotly_url` in
  Tool valves if needed; old saved valve values are not overwritten by new defaults.
- Enable **Settings → Interface → iframe Sandbox Allow Same Origin** for the
  shared lifecycle manager and parent bridges. Rendering itself no longer reads
  the parent chat DOM, but without parent access the manager cannot enforce its
  cross-iframe budget and browser-local state bridges may be unavailable.
  Asset loading remains subject to the host sandbox and CSP.

## New tool contract

```python
visualize_tool_result(
    source_tool_call_id: str,
    instruction: str,
    title: str = "Tool Result Visualization",
    retry_attempt: Literal[0, 1] = 0,
)
```

1. Read the skill, call the data-producing tool, and wait for it to finish.
2. Copy its complete explicit `tool_call_id` / `call_id`, not its tool name.
3. Pass a short `instruction` describing fields, chart type and transformations
   in a later tool round. Do not generate HTML or copy data into arguments.
   Run visualizations sequentially. The loading slot is emitted before the
   internal model request starts; the completed fragment uses `getToolData()`.
4. After the visualizer returns, explain what the chart shows. **Do not emit
   HTML or `@@@VIZ-START` / `@@@VIZ-END` blocks in the text response.**

The fragment contains styles first, content next, and scripts last. Do not use
Markdown fences or document wrapper tags. Declare dependencies with
`data-iv-libraries="chartjs"`, `"plotly"`, or `"chartjs plotly"`. Do not import
the bundles again. Run initialization directly, not from `window.onload` or
`DOMContentLoaded`: admission may happen after those events.

Scripts execute once **per activation**, not once per chat lifetime. Restoring
a suspended visualization starts a new runtime. Initialization should draw from
the saved data and must not submit prompts, rerun producer calls, or perform other
external side effects.

If `status="retry_required"`, call the visualizer once in the next sequential
round using `retry.arguments` unchanged. Do not rerun the
producer. If the bounded retry fails, report the error.

## Internal request and progress

The child receives available `__messages__` history, supplied retrieved sources,
the selected dataset and renderer instructions. Historical tool results are plain
reference messages, not active tool calls. Tool schemas, reasoning fields, private
request metadata and chat/session event routing are not forwarded. This is not a
claim to reproduce the provider's exact original prompt or hidden model state.
Original request state is untouched. Model access checks remain enabled.

Valves: `generation_timeout_seconds=120`, `generation_max_tokens=8000`,
`generation_context_max_chars=400000`. The context ceiling rejects oversized input
without silently truncating it. Generation is non-streaming internally; an incomplete
finish, refusal, tool call or malformed fragment becomes a visible error. There is
no automatic model retry, and browser rendering is not a server-side validation step.

Placeholders are small independent documents: no chart bundles or saved dataset.
They have an absolute expiry (generation timeout plus 30 seconds). Cancellation
tries to persist an error; after process failure, expired placeholders display a
timeout/interruption message on reload. Reload never starts another request.
This is not a durable background job: server restart loses pending generation.

Each update reads the saved message's latest embeds, replaces only the matching
UUID slot and emits the entire preserved array with `replace=True`, then reads
the message back to check that the slot was stored. Updates from
this tool share a per-message app lock and, when `app.state.redis` is configured,
a Redis lease. Read/emit work has a shorter deadline than the lease. Redis errors
fail closed. Without Redis, protection is single-process only. Unrelated writers
that do not take this lock can still race: OWUI has no atomic per-embed update API.
Do not run competing embed-producing tools in the same parallel batch.

## Persistence model

Each successful call creates an `iv-visualization-artifact` JSON payload:

| Field | Meaning |
|-------|---------|
| `schemaVersion` | Saved artifact format, currently 1 |
| `visualizationId` | UUID generated once for this embed; independent of DOM position |
| `runtimeVersion` | Runtime build embedded with this artifact |
| `sourceToolCallId` | Exact original source call ID, for provenance |
| `title`, `html` | Title and complete model-generated fragment |
| `data` | Snapshot of the selected textual source result, including valid null |

The payload is safely JSON-encoded; HTML delimiters and Unicode line separators
are escaped without changing the underlying fragment or data. The runtime
configuration stores the same ID and the selected library URLs.

The final complete document is returned in `HTMLResponse` and emitted inside a
message-level snapshot with `replace=True`; the placeholder is not returned as a
tool result. This preserves the existing two OWUI
mount/storage paths. There is **no separate artifact database or browser-side
chat rewrite** in this stage: durable storage is OWUI's saved embed. Successful
tool execution confirms packaging/emission, not a database commit or successful
execution of model JavaScript.

After a page reload, SPA chat remount, or opening a saved chat in a fresh browser,
the saved embed supplies its HTML and data directly. It does not fetch the
original producer result, search for markers, or depend on a particular embed
index. The server must actually retain the complete embed; temporary/deleted
chats or lost embeds cannot be recovered from nothing.

Schema, identity, missing data, library and script failures produce a visible
error with the visualization ID. The saved source is not overwritten with the
error or with a preview. Format checks do not validate the correctness or safety
of arbitrary generated JavaScript.

### Versions and old chats

This is a breaking tool-contract change: `instruction` replaces `html`. Update the
tool and skill together and reload OWUI after installing.

Existing saved embeds keep their original code and streaming protocol; they are
**not migrated or repaired automatically**. Legacy rendering helpers remain in
the source for regression coverage, but new tool calls always use saved artifacts.
Lifecycle V5 is isolated from older managers; mixed old/new embeds have separate
manager budgets.

The runtime build and asset URLs are recorded, but the files at those URLs are
not copied into the artifact or content-hash pinned. Keep compatible library
bundles at those paths. Updating files can still affect old charts. Offline HTML
exports also require accessible bundles; they are not single-file library archives.

## Source result resolution

The tool searches for an exact source call ID in this order:

1. Active response output for the current assistant message.
2. Saved `message.output` for that message.
3. Completed results in the supplied dialogue.

Only the selected textual result is included; neighboring results, tool
arguments, private metadata, images and attachments are excluded. JSON strings
are normalized to their values; ordinary text remains a string. Later changes to
the source call do not modify a previously packaged embed.

## Runtime and caches

- Both libraries load in parallel when a runtime is admitted. Consumers await
  only their declared dependency; an unused missing Plotly does not block Chart.js.
- Inline scripts run in source order after the fragment is mounted and its
  container has usable dimensions. Bundle/module loading and initial layout
  waits have 12-second failure deadlines. Saved mode reports failures rather
  than silently leaving a spinner; it does not inherit legacy loader retries.
- The default limit is two resident runtimes, including loading and suspending.
  Older frames become restorable previews or placeholders. New and explicitly
  restored graphs retain the existing priority policy; viewport scheduling is
  not part of this stage.
- IndexedDB holds temporary original embed documents under page-session keys.
  An 8 MiB RAM fallback is available if storage is blocked. Cache cleanup on
  unmount/page exit and 24-hour expiry do not delete OWUI's saved embeds.
- Previews have an estimated 16 MiB budget. Failure to capture a preview does not
  remove the saved HTML/data. If neither cache can retain a source, admission
  pauses rather than discarding it. These are cache budgets, not total RAM limits.
- `saveState/loadState` use a visualization-ID prefix for new embeds, avoiding
  collisions between charts in one message. This optional UI state is browser-local
  and does not follow the saved data to another device.
- The generated scripts, observers and known chart instances are disposed when
  the runtime is parked or removed. IndexedDB is a cache, not the backup.

Valves: `max_active_visualizations=2` (0 disables the cap; at most two simultaneous
initializations) and `point_density=1.0` (0 disables the point budget).

For dense lines/scatters, use `ivDownsample(points, {container, x, y, mode, seriesCount})`
on display arrays before plotting. Keep summaries and calculations on the full
`getToolData()` result. No automatic rewriting of generated chart code is performed.

## Security

Saved HTML still contains arbitrary model-generated JavaScript. This change is
about reliable source restoration, not a new security boundary. Same-origin
iframes can access the parent page when the OWUI setting allows it. Strict,
balanced and offline modes restrict script loading to the OWUI origin.

JSON encoding prevents the artifact from breaking out of its inert payload, but
does not sanitize the HTML when it is deliberately executed. Never treat
untrusted shared/imported chat HTML as trusted application code.

## Tests

Run `python3 -m pytest -q inline-visualizer-tool-result/tests`.
Node enables executable JavaScript tests.

With Playwright and Chromium installed, set `IV_BROWSER_TESTS=1`
(optionally `IV_CHROMIUM_PATH` and `NODE_PATH`). Browser regressions cover:

- Legacy 20-iframe admission, failed previews and unmount cleanup.
- Public short-call tool: loading before generation, replacement, expiry and reload.
- Saved renderer embeds mounted without chat source or OWUI IDs.
- Old-chart reactivation, reordered SPA remounts and a full page reload.
- A clean browser context with IndexedDB blocked, simulating loss of caches.
- Independent state keys and visible errors for corrupt/unsupported artifacts,
  missing data, missing bundles and generated-script failures.

Mocked Python integration tests exercise request-state isolation, access-check flags,
model selection, incomplete responses, cancellation, context limits, save failures,
multiple tool instances updating one message, and Redis lease use/failure.

Bundles in these tests are local stubs. They verify the browser lifecycle, not
real Chart.js/Plotly performance or OWUI server persistence. Before deployment,
verify new charts survive an actual OWUI reload, chat switch and second browser
session, including multiple visualizations in one assistant message.

## Errors

- `Invalid source_tool_call_id`: empty/invalid source ID.
- Invalid title/retry: title longer than 300 characters or retry outside 0/1.
- `retry_required`: source is not visible yet; retry once with supplied arguments.
- `Tool result not found`: source is still absent after the retry.
- Generation failure: model error/refusal, incomplete response, invalid fragment,
  context limit or data that cannot be JSON-encoded; replaces loading with an error.
- Loading save failure: no internal model request starts.
- Final save failure: the returned tool-output copy still contains the final
  document, but the response explicitly does not claim durable message-level saving.

Validation failures before loading emit no embed. Once loading was emitted,
generation errors replace that slot. Runtime errors appear inside the final embed.
Use the separate `visualize()` tool for visuals unrelated to completed tool results.
