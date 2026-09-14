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
)
```

`source_tool_call_id` is required. It must be the complete `tool_call_id` or `call_id` copied from the completed source call. It is treated as an opaque string and matched exactly.

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
- `Tool result not found`: no completed result with the exact ID is available in the current conversation.
- `Tool result cannot be serialized`: the selected result cannot be safely encoded as JSON.

When the tool returns an error, do not emit a `@@@VIZ-START` block.

## Standard visualizations

Use the separate `visualize()` tool and its `visualize` skill for diagrams, explainers, widgets, or other visualizations that do not reuse a completed tool result.
