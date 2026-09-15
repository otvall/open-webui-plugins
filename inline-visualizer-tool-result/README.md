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
