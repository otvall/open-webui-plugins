# Inline Visualizer — Tool Result

`visualize_tool_result()` renders an interactive visualization from the textual result of one completed Open WebUI tool call. It is a dedicated companion to the standard `visualize()` tool: the standard tool creates ordinary visualizations, while this tool always requires a source call ID and always exposes that call's result through `getToolData()`.

## Requirements

- Open WebUI 0.10.2 or newer
- **Settings → Interface → iframe Sandbox Allow Same Origin** enabled
- The bundled `visualize-tool-result` skill available through `view_skill`
- Both browser bundles served by Open WebUI: Chart.js at `/static/iv-libs/chart.umd.min.js` and Plotly at `/static/iv-libs/plotly.min.js` (or configure `chartjs_url` and `plotly_url` in Tool valves). The tool does not install these files on the server.

## Tool signature

```python
visualize_tool_result(
    source_tool_call_id: str,
    title: str = "Tool Result Visualization",
    retry_attempt: Literal[0, 1] = 0,
)
```

`source_tool_call_id` is required. It must be the complete `tool_call_id` or `call_id` copied from the completed source call. It is treated as an opaque string and matched exactly.

`retry_attempt` is a bounded recovery control. Omit it on the initial call. If the result is not visible yet, the tool returns `status="retry_required"` with exact `retry.arguments`; call `visualize_tool_result()` once more in the next sequential tool round using those arguments. The retry keeps the original source call ID and sets `retry_attempt=1`.

## Workflow

The producer and visualizer must run in separate tool rounds:

```text
Round 1:
  query_database(...)

Wait for the completed result and copy its exact call ID.

Round 2:
  visualize_tool_result(
      title="Sales",
      source_tool_call_id="<exact copied ID>"
  )
```

After the tool succeeds, emit one visualization block:

```text
@@@VIZ-START
<style>
  /* styles */
</style>

<div id="chart"></div>

<script>
  const data = getToolData();
  // Render data.
</script>
@@@VIZ-END
```

The producer and `visualize_tool_result()` cannot be called in the same parallel batch because the source result is not available until Open WebUI records the completed producer call.

If a provider nevertheless puts them in one batch, the initial visualizer call returns `retry_required` instead of an error. Retry only `visualize_tool_result()` in the next tool round with the supplied arguments. Reuse the existing producer result; never rerun the producer to recover a visualization. A missing result after that single retry becomes `Tool result not found`, preventing an unbounded loop.

## Result resolution

The tool searches for an exact matching call ID in this order:

1. the active response stream for the current assistant message;
2. the saved `message.output` for that message;
3. completed tool results in the current dialogue supplied through `__messages__`.

If an ID occurs more than once, the newest matching result is selected. Only the selected textual result is injected. Tool arguments, metadata, neighboring results, images, and file attachments are excluded.

JSON strings are parsed before injection. Ordinary text remains a string and JSON `null` becomes JavaScript `null`.

## Browser performance

Each iframe first mounts a lightweight shell. A shared lifecycle manager admits
heavy runtimes before data parsing, library loading, or chart initialization.
It coalesces history-mount bursts and prioritizes newer charts and explicit user
restores. The default budget of two includes loading, live, and suspending
iframes, including hidden chat routes. Older, never-started charts show a
**Restore interactivity** placeholder instead of loading libraries just to make
a preview. Raw visualization source remains hidden in the chat.

Chart.js and Plotly start loading in parallel as soon as a runtime is admitted,
without waiting for `@@@VIZ-START`. Do not add imports for either library.
Imports of the same configured URL are deduplicated. Mark inline consumers with
`data-iv-libraries="chartjs"`, `"plotly"`, or `"chartjs plotly"`. Each consumer
waits only for its declared dependencies. For older generated code, direct
`Chart`/`Plotly` references are detected conservatively; explicit attributes are
recommended for aliases or dynamic access. `ivRequireLibraries(['plotly'])`
is also available for asynchronous consumers.

Other referenced libraries are discovered while the visualization block is
still streaming and execute in source order. Inline visualization
code remains deferred until `@@@VIZ-END`, then waits for its required libraries,
any additional imports in source order, and a usable iframe layout.
The loader and "Visualization ready" notification
remain active until that script chain has actually completed, so the first
library load no longer produces a false-ready blank frame.

External and same-origin library loads are retried twice after a transient
failure. If all attempts fail, the iframe shows a script-load error instead of
silently collapsing to an empty rectangle. Under the default `strict` security
level, chart libraries must be served from the Open WebUI origin (for example,
under `/static`); public CDN scripts require `security_level="none"`.
An unavailable Plotly bundle does not delay or block Chart.js-only scripts, and
library-free scripts do not wait for either bundle. A failed required dependency
skips its consumer and produces a visible error.
Updating the tool changes new embeds; existing saved embeds keep their original
startup code and must be regenerated to use this behavior.

After the script chain completes, the wrapper immediately dispatches a resize
and calls the public resize hooks exposed by Chart.js, ECharts, and Plotly.
Canvas-based charts are kept in the loading state until their first painted
frame (with a two-second safety timeout). This prevents responsive line charts
from remaining blank until the surrounding assistant response finishes and
causes Open WebUI to perform another layout pass.

When a saved visualization is mounted inside a hidden SPA chat route, inline
chart code waits until the iframe has a non-zero layout before executing. The
parent lifecycle manager also watches iframe width and repeats the public resize
hooks when a chat becomes visible again.

Before admitting another runtime, the manager parks an older eligible iframe.
Preview capture has a 1.5-second deadline; a failed snapshot produces a restorable
placeholder and still releases the runtime. Explicit **Load visualization** or
**Restore interactivity** requests can pause an initializing iframe to avoid
waiting forever behind a stalled stream. Automatic admission does not interrupt
initializing charts. At most two runtimes initialize concurrently even when
`max_active_visualizations=0` disables the resident-runtime cap.

Preview retention is capped at an estimated 16 MiB (encoded URL plus decoded
pixels); older images are replaced with restore buttons. Source HTML, including
the selected dataset, is cached separately in same-origin IndexedDB under a
page-session key. Sources are deleted when records are removed or the page exits
(best effort); abandoned entries expire after 24 hours and are swept on the next
cache open. This cache is local to the browser and contains the same data as the
embed; it is not encrypted. If IndexedDB is unavailable, a bounded 8 MiB RAM
fallback is used. If neither store can preserve a source, admission pauses
instead of discarding data or exceeding the runtime budget. Lightweight shells
and Open WebUI's own chat state are outside these cache budgets.

Theme observers and cross-document callbacks are disposed before parking and on
unmount. Detached text nodes are pruned from the shared source-hiding registry.
Lifecycle version 4 is isolated from older managers; old saved embeds must be
regenerated, and a full page reload is recommended after upgrading.

### Regression tests

Run `pytest -q inline-visualizer-tool-result/tests`. Node enables executable JS
tests. With Playwright and Chromium installed, set `IV_BROWSER_TESTS=1` to include
the 20-iframe browser regression (optionally set `IV_CHROMIUM_PATH`). The browser
test uses local mocked library responses; it covers admission, missing Plotly,
IndexedDB restoration, failed preview capture, and SPA unmount cleanup, not live
Open WebUI integration or real chart-library performance.

Configure this behavior in the Tool valves:

- `max_active_visualizations` defaults to `2` (`0` disables suspension; maximum `10`).
- `point_density` defaults to `1.0` display point per CSS pixel (`0` disables point budgeting; maximum `4`).

`getToolData()` always returns the complete source result. For dense line and scatter series, reduce only the display array:

```js
const rows = getToolData();
const chart = document.getElementById('chart');
const displayRows = ivDownsample(rows, {
  container: chart,
  x: 'date',
  y: 'value',
  mode: 'line',
  seriesCount: 1
});
```

`ivPointBudget(container, seriesCount)` returns the per-series budget. `ivDownsample(points, options)` uses LTTB for lines and spatial binning for scatter plots, never mutates the source array, and falls back to endpoint-preserving even sampling when coordinates are invalid. Keep calculations and aggregates on the original `rows` value.

## Security

The selected result is serialized into a non-executable `<script type="application/json">` element. HTML delimiters and Unicode line separators are escaped before insertion. A result that cannot be serialized produces a controlled error and no iframe is mounted.

## Errors

- `Invalid source_tool_call_id`: the required ID is empty or invalid.
- `Invalid retry_attempt`: the retry control is outside its supported `0`/`1` range.
- `retry_required`: the initial call could not see the source result yet; retry the visualizer once with the supplied arguments.
- `Tool result not found`: the single visualization retry still could not resolve the exact ID.
- `Tool result cannot be serialized`: the selected result cannot be safely encoded as JSON.

When the tool returns `retry_required` or an error, do not emit a `@@@VIZ-START` block.

## Standard visualizations

Use the separate `visualize()` tool and its `visualize` skill for diagrams, explainers, widgets, or other visualizations that do not reuse a completed tool result.
