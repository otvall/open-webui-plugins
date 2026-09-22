# Inline Visualizer

This context covers visualizations of JSON results produced by tools in an Open WebUI chat.

## Language

**Source result**:
A completed JSON object or array from a tool call in the chat, identified by its exact call ID.
_Avoid_: Dataset copy, chart data argument

**Visualization**:
A single chart of a source result embedded in an assistant message, with axes, labels, and a legend. It can contain multiple series and support tooltips, zooming, and toggling series through the legend.
_Avoid_: Dashboard, data table, streamed markup, raw HTML response

**Loading placeholder**:
The temporary view shown in a visualization's place while it is being prepared.
_Avoid_: Partial visualization
