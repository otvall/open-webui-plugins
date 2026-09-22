---
name: visualize
description: Render one interactive chart from a JSON tool result in a saved Open WebUI chat. Call visualize(source_tool_call_id=...) only for explicit chart requests after a Native tool call. Use local Chart.js or Plotly and getToolData() for the source data.
---

# Inline Visualizer

A visualization is one chart with axes, labels, and a legend. Multiple curves or series share the same plotting area.

## Use the tool

1. Identify the exact `tool_call_id` of a completed JSON-producing Native tool call in this saved chat. If no completed JSON result and exact call ID are available, do not call `visualize`.
2. Call `visualize(source_tool_call_id="…", title="…")`. The tool validates the result, shows a loading placeholder, and asks the selected model to generate the chart using the available conversation and selected result.
3. After success, briefly describe the chart in ordinary text. The tool embeds the generated HTML; do not print HTML, code fences, or visualization delimiters in the chat response.
4. If the tool returns an error, explain it. Do not automatically rerun the source tool or visualization generation. If the user requests separate charts, call the tool sequentially; each visualization still contains one chart.

## Chart content

- Render only the plotting area, axes and ticks, concise labels with units, and a legend identifying the series. Keep explanations in the chat response.
- Use one plotting area, even when there are several curves. Do not create subplots, dashboards, tables, metric cards, forms, tabs, filters, sliders, or separate buttons and toolbars.
- Allow hover tooltips, zooming within the plot, and hiding/showing series through the chart's legend. These interactions belong to the chart itself.
- The runtime supplies download controls, loading feedback, and errors. Do not recreate these controls or add chat/navigation actions.

## Local libraries

Choose one chart library per visualization and load only its exact root-relative URL:

| Library | Script URL | Use |
| --- | --- | --- |
| Chart.js | `/static/chart.umd.min.js` | Basic line, bar, or scatter charts with tooltips and a legend |
| Plotly | `/static/plotly.min.js` | Charts needing built-in zoom or more advanced plotting |

Use only these two chart libraries. No CDN URLs, additional libraries, plugins, date adapters, external fonts, or remote fallback loaders. If a required script fails to load, let the runtime show the error.

The runtime separately loads `/static/html2canvas.min.js` when needed for PNG export. Generated chart code must not load it. All three files are served by the same Open WebUI instance.

### Chart.js

Load `<script src="/static/chart.umd.min.js"></script>` before the script using `Chart`. Put the canvas in a dedicated container with `position: relative` and an explicit height, such as `360px`. Set `responsive: true` and `maintainAspectRatio: false`.

Use the built-in tooltip and legend. For dates, use category labels or numeric timestamps without an external date adapter. Choose Plotly when zoom is needed instead of adding a Chart.js plugin.

### Plotly

Load `<script src="/static/plotly.min.js"></script>` before the script using `Plotly`. Give the plot container an explicit height and `width: 100%`.

Use one plotting area and configure `Plotly.newPlot(container, traces, layout, config)` with:

- `layout.showlegend: true` and meaningful trace names.
- `config.responsive: true`, `config.displayModeBar: false`, and `config.displaylogo: false`.
- `config.scrollZoom: true` for zooming without a toolbar; retain built-in hover and legend interactions.

Do not add range sliders, range selectors, update menus, or extra plotting areas.

## Data and initialization

Return one complete HTML fragment: `<style>` first, the chart container next, library script then initialization script last. Omit document wrapper tags and Markdown fences. Initialize directly; do not wait for `DOMContentLoaded` or `window.onload`.

`getToolData()` returns a Promise with a fresh parsed JSON copy of the selected source result. Await it before building the chart. Derive series from that result; never copy its rows or arrays into the generated fragment. The internal model sees the available conversation and selected result to choose the fields and chart type.

Data can come from the current answer or an earlier answer in the same saved chat. Open WebUI 0.11.1 needs Native function calling and **Allow iframe same origin** enabled in User Settings → Interface. Data-loading and rendering failures are displayed by the runtime; do not hide exceptions or substitute invented data.

## Chart appearance

Use a flat, uncluttered chart with no decorative cards, gradients, shadows, or emoji. Keep labels readable (at least 11px), use regular text with moderate emphasis, and format displayed numbers without changing the underlying data.

The runtime supplies theme-aware CSS variables. Use `--color-text-primary` for text, `--color-text-secondary` for ticks, `--color-border-tertiary` for grid lines, and `--color-bg-primary` for the background. For library options expecting concrete colors, resolve variables with `getComputedStyle(...)` rather than passing CSS `var(...)` strings into canvas drawing options.

Use distinct series colors consistently across curves, tooltips, and legend entries. Suggested sequence: `#1D9E75`, `#7F77DD`, `#D85A30`, `#378ADD`, `#BA7517`. Leave sufficient margins for axis labels and the legend.
