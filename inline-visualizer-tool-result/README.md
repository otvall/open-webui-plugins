# Inline Visualizer — Tool Result

`visualize_tool_result()` renders an interactive visualization from the textual result of one completed Open WebUI tool call. It is a dedicated companion to the standard `visualize()` tool: the standard tool creates ordinary visualizations, while this tool requires a source tool name and exposes its latest call's result through `getToolData()`.

## Requirements

- Open WebUI 0.10.2 or newer
- **Settings → Interface → iframe Sandbox Allow Same Origin** enabled
- The bundled `visualize-tool-result` skill available through `view_skill`

## Tool signature

```python
visualize_tool_result(
    source_tool_name: str,
    title: str = "Tool Result Visualization",
    retry_attempt: Literal[0, 1] = 0,
)
```

`source_tool_name` is required: pass the exact function name, such as `query_database`. Matching is case-sensitive and preserves namespaces. The visualizer selects the latest invocation of that function in the current dialogue; the model does not need access to call IDs. Version 1.2.0 replaces the previous ID-based argument; update the installed tool and bundled skill together.

`retry_attempt` is a bounded recovery control. Omit it on the initial call. If the result is not visible yet, the tool returns `status="retry_required"` with exact `retry.arguments`; call `visualize_tool_result()` once more in the next sequential tool round using those arguments. The retry keeps the source tool name and sets `retry_attempt=1`.

## Workflow

The producer and visualizer must run in separate tool rounds:

```text
Round 1:
  query_database(...)

Wait for the completed result.

Round 2:
  visualize_tool_result(
      title="Sales",
      source_tool_name="query_database"
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

If the latest source call is visible but its result is not ready, the initial visualizer call returns `retry_required` instead of an error. Retry only `visualize_tool_result()` in the next tool round with the supplied arguments. Reuse the existing producer result; never rerun the producer to recover a visualization. A missing result after that single retry becomes `Tool result not found`, preventing an unbounded loop.

## Result resolution

The tool searches for the latest invocation with the exact function name in this order:

1. the active response stream for the current assistant message;
2. the saved `message.output` for that message;
3. calls and results in the current dialogue supplied through `__messages__`.

Latest means invocation order, even when parallel calls finish in a different order. Once the latest matching call is found, a pending or missing result triggers the single retry rather than falling back to older data. Another snapshot can supply that same call's result only within the same assistant message. History without a message ID cannot be used to complete a call selected from the active/stored message, because call IDs may repeat across turns.

Supported formats include Responses API call/output pairs (direct or nested in `message.output`), Chat Completions `assistant.tool_calls` paired with `role="tool"` messages, and result messages carrying an explicit `name`. When call records are absent, named results are ordered by their appearance in the history. Names are never inferred from IDs or result text.

Only the selected textual result is injected. Tool arguments, metadata, neighboring results, images, and file attachments are excluded.

JSON strings are parsed before injection. Ordinary text remains a string and JSON `null` becomes JavaScript `null`.

## Browser performance

Referenced chart libraries, including same-origin `/static` assets, are
discovered and loaded while the visualization block is still streaming. Inline
visualization code remains deferred until `@@@VIZ-END`, then runs behind those
imports in source order. The loader and "Visualization ready" notification
remain active until that script chain has actually completed, so the first
library load no longer produces a false-ready blank frame.

After the script chain completes, the wrapper immediately dispatches a resize
and calls the public resize hooks exposed by Chart.js, ECharts, and Plotly.
Canvas-based charts are kept in the loading state until their first painted
frame (with a two-second safety timeout). This prevents responsive line charts
from remaining blank until the surrounding assistant response finishes and
causes Open WebUI to perform another layout pass.

The tool keeps at most two completed visualizations interactive by default. On page reload, completed visualizations are ranked by their stable order in the chat, so the newest two remain interactive regardless of iframe load timing. When another visualization completes, the oldest eligible iframe is captured at CSS-pixel resolution and replaced with a static preview. Click the preview or **Restore interactivity** to reload it immediately; after that iframe finishes rendering, the oldest eligible live visualization is suspended in exchange. Streaming visualizations are never suspended.

Static previews are session-local. They are regenerated after a page reload and are not written to the chat or IndexedDB. Preview capture waits once for imported scripts, chart animation, and the built-in fade-in to settle. Canvas-first charts are captured directly, and blank raster output falls back to the secondary capture path instead of being frozen as a white rectangle. The lifecycle manager applies only to visualizations created by builds that provide lifecycle version 1; older saved embeds are not migrated.

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

- `Invalid source_tool_name`: the required function name is empty or invalid.
- `Invalid retry_attempt`: the retry control is outside its supported `0`/`1` range.
- `retry_required`: the initial call could not see the source result yet; retry the visualizer once with the supplied arguments.
- `Tool result not found`: the single visualization retry still could not resolve the latest call's result for this function name.
- `Tool result cannot be serialized`: the selected result cannot be safely encoded as JSON.

When the tool returns `retry_required` or an error, do not emit a `@@@VIZ-START` block.

## Standard visualizations

Use the separate `visualize()` tool and its `visualize` skill for diagrams, explainers, widgets, or other visualizations that do not reuse a completed tool result.
