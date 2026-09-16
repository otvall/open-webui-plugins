# Inline Visualizer — Tool Result

`visualize_tool_result()` renders an interactive visualization from the textual result of one completed Open WebUI tool call. It is a dedicated companion to the standard `visualize()` tool: the standard tool creates ordinary visualizations, while this tool always requires a source call ID and always exposes that call's result through `getToolData()`.

## Requirements

- Open WebUI 0.10.2 or newer
- **Settings → Interface → iframe Sandbox Allow Same Origin** enabled
- The bundled `visualize-tool-result` skill available through `view_skill`

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

The wrapper is returned as an inline `HTMLResponse` through Open WebUI's normal
tool-result pipeline. This ensures the embed is attached to the completed
`function_call_output` before the frontend receives the corresponding
`chat:completion` update; emitting an early standalone `embeds` event can be
persisted while still being missed by the live page.

## Browser performance

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

- `Invalid source_tool_call_id`: the required ID is empty or invalid.
- `Invalid retry_attempt`: the retry control is outside its supported `0`/`1` range.
- `retry_required`: the initial call could not see the source result yet; retry the visualizer once with the supplied arguments.
- `Tool result not found`: the single visualization retry still could not resolve the exact ID.
- `Tool result cannot be serialized`: the selected result cannot be safely encoded as JSON.

When the tool returns `retry_required` or an error, do not emit a `@@@VIZ-START` block.

## Standard visualizations

Use the separate `visualize()` tool and its `visualize` skill for diagrams, explainers, widgets, or other visualizations that do not reuse a completed tool result.
