# Inline Visualizer

This context covers visualizations of JSON results produced by tools in an Open WebUI chat.

## Language

**Source result**:
A completed JSON object or array from a tool call in the chat, identified by its exact call ID.
_Avoid_: Dataset copy, chart data argument

**Visualization**:
An interactive view of a source result embedded in an assistant message.
_Avoid_: Streamed markup, raw HTML response

**Loading placeholder**:
The temporary view shown in a visualization's place while it is being prepared.
_Avoid_: Partial visualization
