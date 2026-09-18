---
name: visualize-tool-result
description: Render one completed tool call's result as a focused visualization using visualize_tool_result(). Use only when a system or developer instruction explicitly requires a visualization based on another tool's completed result. Use the standard visualize skill for all other visualizations
---

# Inline Visualizer — Tool Result

This handbook explains how to render the result of one completed tool call directly inline in chat using `visualize_tool_result()`.

## How to use

Use this skill only when a system or developer instruction explicitly tells you to visualize the result of another tool call. Do not choose this workflow on your own. For ordinary diagrams, explainers, widgets, or visuals that do not reuse a completed tool result, use `visualize()` and the standard `visualize` skill instead.

1. Call the data-producing tool and wait until it has fully completed.
2. Read its explicit `tool_call_id` or `call_id` from the structured conversation context and copy it character-for-character.
3. Describe the visualization briefly: chart type, fields, units, transformations and necessary filters. Do not write HTML or repeat the dataset.
4. In a later sequential tool round call `visualize_tool_result(source_tool_call_id="<exact ID>", instruction="Plot monthly revenue using month and revenue fields", title="…")`. Leave `retry_attempt=0`. Call visualizations sequentially, not in parallel. YOU MUST CALL THE TOOL.
5. If the result is `status="retry_required"`, retry once in the next tool round with `retry.arguments` unchanged. Never rerun the producer solely for visualization recovery. Do not automatically retry generation failures: another call incurs another model request.
6. After success, briefly explain what the chart shows. Do not output its HTML in the assistant message.

The tool first displays “Загрузка визуализации…”, then makes a separate server-side model request using the available conversation context and selected result, without tools. The renderer creates HTML internally. The placeholder is replaced with a versioned, self-contained embed holding HTML, the data snapshot and the same permanent visualization ID. Rendering and restoration do not call a model, read the chat DOM or depend on browser caches. Interrupted placeholders expire visibly; reloading never starts another paid request.

This workflow requires native function calling, a saved chat and a server-connected model. The administrator may set `generation_model_id`; an empty value uses the current model. Pipe, Arena and browser-direct models are not supported for the internal request. Oversized context fails rather than silently dropping history. Tool success does not prove browser rendering succeeded.

The rest of this handbook is **renderer reference**, not a request for the main assistant to generate HTML. Runtime-critical generation rules are also embedded in `tool.py`, because OWUI installs it separately from this skill.

**Internal HTML rules:**

- Pass a fragment only: styles first, visible content next, scripts last; no document wrapper tags or Markdown fences.
- Do not use `@@@VIZ-START` / `@@@VIZ-END`. The complete fragment is the internal model's response, not a main-model argument or a later chat response.
- Initialize scripts directly. Do not wait for `window.onload` or `DOMContentLoaded`, which may have already fired when a parked runtime starts.
- Declare `data-iv-libraries="chartjs"`, `"plotly"`, or `"chartjs plotly"` on scripts using those libraries. They are already preloaded; do not import libraries.
- Scripts execute once per runtime activation. Restoring a parked graph creates a fresh runtime: avoid network requests, chat submissions, or other side effects during initialization.
- Use `saveState/loadState` only for necessary local UI settings. They are separate from the saved data snapshot and do not synchronize to other devices.
- Enable **iframe Sandbox Allow Same Origin** in OWUI for the shared lifecycle manager and optional parent bridges. Without parent access, self-contained rendering can run independently, but cross-iframe admission and state bridges are unavailable; library loading still depends on the host's sandbox/CSP.
- Browser-render errors appear inside the embed. Tool success means the embed was constructed, not that the model's JavaScript has been verified to render correctly.
- Old streaming embeds are not migrated by updating the tool or skill. Never claim a missing historical graph has been repaired automatically.

## What's auto-injected

- Theme CSS, SVG classes, color ramps, height reporting, sendPrompt() bridge, and openLink() bridge
- The `getToolData()` bridge containing only the selected completed call's textual result
- Chart.js (`window.Chart`) and Plotly (`window.Plotly`), loaded in parallel when the runtime is admitted. Mark consumers with `data-iv-libraries="chartjs"`, `"plotly"`, or `"chartjs plotly"`; they wait only for their dependencies. Do not add imports for these libraries.
- `ivPointBudget()` and `ivDownsample()` for density-limited line and scatter rendering
- Automatic suspension of older completed visualizations; static previews can be reactivated by the user
- Theme-aware bare-tag form elements for filters required by the task (see below)

## Source call ID

`source_tool_call_id` is required. It is the ID of a completed tool call, not the name of the tool.

Treat the identifier as an opaque token. Copy it exactly as it appears on the completed source call. Never construct, infer, normalize, shorten, extend, or repair it. Preserve every prefix, separator, and numeric suffix.

For example, if the completed call explicitly contains `tool_call_id = "functions.get_sales:1"`, pass exactly `source_tool_call_id="functions.get_sales:1"`. Do not pass `get_sales`, `get_sales:1`, `function.get_sales:1`, or `functions.get_sales`.

The example demonstrates exact copying only. It does not define a universal ID format. If no explicit `tool_call_id` or `call_id` is available, do not guess one and do not call `visualize_tool_result()`.

## Result handling

You may inspect the completed producer result to understand its field names, nesting, and value types. Access the actual dataset only through `getToolData()`:

```js
const data = getToolData();
```

The value is already parsed when possible: JSON strings become objects, arrays, scalars, or `null`, while ordinary text remains a string. Do not call `JSON.parse()` unless the producer intentionally returned JSON encoded inside another string. Do not reproduce the producer result in the tool arguments, generated HTML, generated JavaScript, or another model-generated JSON object.

Each visualization accepts exactly one `source_tool_call_id`. If the visualization requires several related datasets, have one producer call return them together as a combined result.

Never call the producer and `visualize_tool_result()` in the same parallel batch. The result is unavailable until the producer call has finished and Open WebUI has recorded it. If the provider batches them despite this rule, follow the returned `retry.arguments` in the next round.

`retry_required` is a bounded visualization retry, not a request for fresh data. Call only `visualize_tool_result()` with the supplied arguments. If that retry returns `Tool result not found`, stop visualization recovery and report that the existing result could not be resolved. For `Invalid source_tool_call_id`, re-read the explicit ID attached to the completed source call; never construct or repair it.

## Dense line and scatter data

`getToolData()` always returns the full source result. Use the full value for totals, averages, thresholds, annotations, and every other calculation. For each line or scatter series whose length exceeds its display budget, you MUST pass a separate display array through `ivDownsample()` before giving it to Chart.js, Plotly, or an SVG path generator.

The default budget is one displayed point per CSS pixel across the chart. Multiple visible series share it. `ivDownsample()` does not mutate its input, uses LTTB for `mode: 'line'`, and uses spatial binning for `mode: 'scatter'`.

Single series:

```js
const rows = getToolData();
const chartEl = document.getElementById('chart');
const displayRows = ivDownsample(rows, {
  container: chartEl,
  x: 'date',
  y: 'value',
  mode: 'line',
  seriesCount: 1
});
// Calculate summaries from rows; draw only displayRows.
```

Multiple series:

```js
const payload = getToolData();
const chartEl = document.getElementById('chart');
const seriesCount = payload.series.length;
const displaySeries = payload.series.map(series => ({
  ...series,
  points: ivDownsample(series.points, {
    container: chartEl,
    x: 'date',
    y: 'value',
    mode: 'line',
    seriesCount
  })
}));
```

`x` and `y` may be an object field name, an array index, or an accessor function. Sort time-series points by x before downsampling. If `ivPointBudget(chartEl, seriesCount)` is greater than or equal to the series length, using `ivDownsample()` is harmless and returns a shallow copy. Do not downsample categorical bars, tables, KPI calculations, or source data merely because they contain many rows.

### Pre-styled form elements

When the task needs a filter, bare `input`, `select`, `button`, and `label` elements receive theme-aware styling. A class or inline style opts out of those defaults; other attributes, including `aria-*`, do not. Label controls and preserve keyboard focus indicators.

The default accent is purple. The optional `data-accent` attribute on an existing control or container selects purple, teal, coral, pink, gray, blue, green, amber, or red; colors adapt to light/dark mode. Do not add a wrapper just for decoration.

## Output rules

- **Chart first:** a chart request produces the chart, not a mini-dashboard.
- Keep a concise title, axes, units, legend, tooltips, and filters necessary for the task. Omit elements that do not help read or operate the chart.
- Put explanations and conclusions in the chat response, outside the visualization.
- Do not add KPI strips, metric cards, extra mini-charts, decorative icons, badges, conversation buttons, or outer card backgrounds/borders around an ordinary chart. A plain sizing container is still required where the chart library needs it.
- Add dashboards or supplementary panels only when the user explicitly requests them. Include only elements serving that request.
- Preserve useful chart interaction, such as tooltips, legend toggles, and necessary filters; do not add animation or controls merely to decorate the output.
- **Flat design:** no gradients, drop shadows, blur, glow, or noise textures.
- Use sentence case, readable numbers (`toLocaleString` or `Intl.NumberFormat`), fonts of at least 11px, and weights 400 or 500.
- Standalone SVG diagrams are valid when they represent the requested information. Do not append them as chart decoration.

---

## Design system

### CSS variables (auto-injected — prefer these so light/dark mode just works)

The tool injects theme-aware CSS variables that adapt to light/dark mode automatically. Use them by default for text, surface, and border colors; reach for a specific hex only when the design genuinely calls for a fixed color (a brand mark, a deliberate accent that shouldn't track the theme).

| Token | Purpose |
|-------|---------|
| --color-text-primary | Main text |
| --color-text-secondary | Labels, muted text |
| --color-text-tertiary | Hints, placeholders |
| --color-text-info/success/warning/danger | Semantic text |
| --color-bg-primary | Main background |
| --color-bg-secondary | Cards, surfaces |
| --color-bg-tertiary | Page background |
| --color-border-tertiary | Default borders (0.15 alpha) |
| --color-border-secondary | Hover borders (0.3 alpha) |
| --font-sans | Default font |
| --font-mono | Code font |
| --radius-md / --radius-lg / --radius-xl | 8px / 12px / 16px |

### Color ramps (9 ramps, auto light/dark)

Each ramp provides fill, stroke, and text variants that adapt to the theme automatically via CSS classes.

| Ramp | 50 (light fill) | 200 | 400 | 600 (light stroke) | 800 (light title) |
|------|------|------|------|------|------|
| purple | #EEEDFE | #AFA9EC | #7F77DD | #534AB7 | #3C3489 |
| teal | #E1F5EE | #5DCAA5 | #1D9E75 | #0F6E56 | #085041 |
| coral | #FAECE7 | #F0997B | #D85A30 | #993C1D | #712B13 |
| pink | #FBEAF0 | #ED93B1 | #D4537E | #993556 | #72243E |
| gray | #F1EFE8 | #B4B2A9 | #888780 | #5F5E5A | #444441 |
| blue | #E6F1FB | #85B7EB | #378ADD | #185FA5 | #0C447C |
| green | #EAF3DE | #97C459 | #639922 | #3B6D11 | #27500A |
| amber | #FAEEDA | #EF9F27 | #BA7517 | #854F0B | #633806 |
| red | #FCEBEB | #F09595 | #E24B4A | #A32D2D | #791F1F |

### Chart dataset colors (use 400 stops)

| Series | Color | Hex |
|--------|-------|-----|
| 1 | teal-400 | #1D9E75 |
| 2 | purple-400 | #7F77DD |
| 3 | coral-400 | #D85A30 |
| 4 | blue-400 | #378ADD |
| 5 | amber-400 | #BA7517 |

For area/line fills, use same color at 20% opacity.

---

## SVG setup

For an SVG that directly represents the requested data or relationships, use the setup below. Include arrow definitions only when the diagram has connectors:

<svg width="100%" viewBox="0 0 680 H">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke"
        stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>
</svg>

- viewBox width **always 680** — set H to **tightly fit** content (last element bottom + 40px). **Never oversize** — calculate the actual bottom of your last SVG element and add 40px. An SVG with content ending at y=180 must use H=220, not 500
- Safe area: x=40 to x=640
- Background transparent — host provides container

### SVG classes (auto-injected)

Drop these on SVG elements instead of writing inline fill, stroke, or
font-size. They track the theme automatically.

| Class | What it is | When to use |
|-------|------------|-------------|
| .t | 14px primary-color text | Default for any visible label inside a node, axis tick, or callout. |
| .ts | 12px secondary-color text | Subtitles, captions, units (e.g. "users", "ms"), supporting text under a .t label. |
| .th | 14px primary text, 500 weight | Node titles and emphasized data labels. |
| .box | Neutral rect — secondary bg, tertiary border | A region that encodes grouping or containment in a standalone diagram; not a decorative chart wrapper. |
| .node | Cursor-pointer + hover opacity on a <g> | Only for a diagram node with an actual task-required interaction. |
| .arr | 1.5px stroke matching theme borders | Arrow lines and connectors. Combine with marker-end="url(#arrow)". |
| .leader | 0.5px dashed guide line | Pulling a label to a part of an illustration when the label can't sit on top of it. |
| .c-{ramp} | Sets fill/stroke + text colors on a whole <g> from one of the 9 color ramps | Color a node by category — apply .c-teal (etc.) to a <g> and every shape and text inside picks up the matching ramp. Un-classed, un-filled <path>/<polygon> children (pie wedges, areas) take the ramp's series color; on a classed or filled mark, fill="currentColor" opts back in. |

### Sizing text inside boxes

Browsers don't auto-size SVG boxes to text. To pick a width, estimate
the rendered glyph width per character and size the box from the
longest line.

- 14px text (.t, .th) → ~8 px / character
- 12px text (.ts) → ~7 px / character
- box_width = max(title_chars × 8, subtitle_chars × 7) + 24 (12 px padding each side)

### Centering text in boxes

<text> defaults to dominant-baseline="alphabetic" — y is the text's
baseline, not its center, so a label placed at the vertical midpoint of
a box actually sits ~4 px too high. For text inside a node, callout, or
any rounded rect, add dominant-baseline="central" and put y at the
box midpoint.

Keep the default (no dominant-baseline) for text that's *meant* to sit
on a baseline: axis tick labels (resting on the axis line), legend labels
(aligned to the swatch baseline), and anything where the bottom edge of
the glyphs is the visual anchor. Setting central on those will make
them look ~4 px low instead.

---

## Diagram types

Use these patterns for standalone diagrams that answer the task, not as extra elements around a chart.

### Flowchart — sequential steps, decisions

- Max **4–5 nodes** per diagram — 6+ → decompose into overview + sub-flows
- Box spacing: 60px between boxes, 24px padding inside
- Single-line node: height 44px, two-line: 56px
- Arrows must not cross any box — use L-bends if needed
- Use marker-end="url(#arrow)" on arrow paths

Single-line node:

<g class="c-teal">
  <rect x="100" y="20" width="180" height="44" rx="8"/>
  <text class="th" x="190" y="42" text-anchor="middle" dominant-baseline="central">Label</text>
</g>

Two-line node:

<g class="c-teal">
  <rect x="100" y="20" width="200" height="56" rx="8"/>
  <text class="th" x="200" y="38" text-anchor="middle" dominant-baseline="central">Title</text>
  <text class="ts" x="200" y="56" text-anchor="middle" dominant-baseline="central">Subtitle</text>
</g>

### Architecture — nested regions, layered systems

For diagrams that show **what contains what**: services inside zones,
modules inside layers, components inside subsystems. The nesting itself
is the information — outer regions are the system, inner regions are
the parts.

- Outermost container: rx=20–24, lightest ramp fill (the 50 stop), 0.5px stroke
- Inner regions: rx=8–12, a darker stop of the same ramp — or a different ramp when the inner region is semantically distinct (e.g. external service inside an internal cluster)
- 20px minimum padding between an inner region's bounds and its parent's edge
- Max 2–3 nesting levels — beyond that, decompose into a top-level overview plus sub-diagrams

### Illustrative — explain a mechanism by drawing it

For "how does this actually work" topics where the answer is spatial:
how light refracts through a prism, how a transformer attention head
weighs tokens, how a heat pump moves heat against a gradient. **Draw
the thing itself**, not a labeled diagram about it.

- Shapes are freeform — paths, ellipses, polygons, curves — not just rounded rects
- Color encodes intensity or state, not category: warm ramps for active / hot / energized, cool ramps for calm / cold / passive, gray for neutral / inert
- Labels live outside the object connected via .leader lines — reserve a ~140px gutter on the side you'll label from
- Add a control only when manipulating that parameter is part of the requested explanation.

---

## Charts (Chart.js)

Chart.js and Plotly are loaded automatically in parallel when the lifecycle
manager admits the iframe runtime. Use `Chart` or `Plotly` directly; declare
`data-iv-libraries="chartjs"` or `data-iv-libraries="plotly"` on the consumer
script (space-separated for both). It runs after the saved fragment is mounted and its
declared dependencies are ready. Do not emit another loader for either library.
An unused library's failure does not block your script. A required library's
failure produces an error; the administrator must correct its asset URL.

Setup pattern:

<div style="position: relative; height: 300px;">
  <canvas id="chart"></canvas>
</div>
<script data-iv-libraries="chartjs">
const ctx = document.getElementById('chart').getContext('2d');
const s = getComputedStyle(document.documentElement);
const textColor = s.getPropertyValue('--color-text-secondary').trim();
const gridColor = s.getPropertyValue('--color-border-tertiary').trim();

new Chart(ctx, {
  type: 'bar',
  data: {
    labels: ['Q1','Q2','Q3','Q4'],
    datasets: [{ label: 'Revenue', data: [12,19,8,15],
      backgroundColor: '#1D9E75', borderRadius: 4, borderSkipped: false }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: textColor, font: { size: 12 } } } },
    scales: {
      x: { grid: { display: false }, ticks: { color: textColor, font: { size: 12 } }, border: { color: gridColor } },
      y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 12 } }, border: { display: false } }
    }
  }
});
</script>

**Chart rules:**
- Wrap canvas in container with position: relative and explicit height — without it, maintainAspectRatio: false collapses the canvas to zero
- Always pass responsive: true, maintainAspectRatio: false in options — without maintainAspectRatio: false, Chart.js locks the canvas to a 2:1 aspect and ignores the container height; without responsive: true, it won't redraw when the iframe re-measures. You have to set them explicitly on every new Chart(...) call (Chart.js reads options at construction time, so there's no global default we could pre-set for you).
- Read CSS variables for text/border colors so the chart tracks the theme
- borderRadius: 4 on bars
- Line charts: tension: 0.3 for smooth curves
- Doughnut: cutout: '60%' — never use pie

**Chart type selection:**

| Data shape | Type | Notes |
|-----------|------|-------|
| Categories + values (a few items, comparable magnitudes) | **Bar** | Default for "compare values across labels". Switch to a horizontal bar (indexAxis: 'y') when labels are long, when there are 8+ categories, or when ranking is the point. |
| Time series, anything sampled at regular intervals | **Line** | tension: 0.3 for a natural curve. Stack multiple datasets when you're comparing trends, not when each line wanders independently — overlap gets unreadable past 4 lines. |
| Parts of a whole, ≤5 slices | **Doughnut** | Use cutout: '60%' so the empty middle can hold a total or label. Skip if the segments are very uneven (one slice >70%) — the small slices vanish; show a stacked bar instead. |
| Two continuous variables, looking for correlation | **Scatter** | Add a trend line if the relationship is the takeaway. For dense clouds, drop point opacity to 0.3–0.5 so density reads. |
| Stacked / cumulative composition over time | **Stacked bar / stacked area** | Bar when the buckets are discrete (months, segments); area when the underlying signal is continuous. |
| Single-value vs target / threshold | **Bar with reference line** | If a chart adds no useful comparison, explain the number in the chat rather than adding a metric card. |
| Multi-dimensional comparison (3–6 axes) | **Radar** | Only when the axes are genuinely commensurate — otherwise a small-multiples bar grid is clearer. |

### Inline SVG charts (no library)

Use inline SVG as the chart itself when the data is small and the shape is simple.
Do not add supplementary SVG graphics around a Chart.js or Plotly chart.
Reach for Chart.js when you need axes, tooltips, hover, animation, or
many series.

**Good fits for inline SVG:**
- **Progress / completion bar** — a value rendered against a fixed track, often paired with a percentage label to its right
- **Ranking strip** — a small number of horizontal bars stacked vertically, each bar a different category color, sized by value
- **Stacked composition row** — one horizontal bar split into colored segments to show parts of a whole, when a doughnut would feel heavy
- **Custom-shape charts** — anything where the chart shape is part of the metaphor (a thermometer for temperature, a battery for charge, a fuel gauge, a tide-line)

**Theme consistency for inline SVG:**
- Use the .t / .ts / .th classes on <text> for labels, captions, and headlines.
  They pick up the theme's text colors and typography scale automatically.
  Never set font-size or fill on label text manually unless you need a specific deviation.
- For neutral backgrounds (track behind a progress bar, empty slot in a ring), use fill="var(--color-bg-secondary)" so it tracks the host theme.
- For data colors, prefer the chart-dataset 400-stop hexes from the table above — they're calibrated to read on both light and dark backgrounds.
  If you need a *whole group* recolored (rect + label + stroke together), wrap it in a <g class="c-teal"> (or any of the 9 ramp classes) and let the SVG class system handle fill + stroke + text in one shot.
- Keep stroke-widths to 0.5 px for chrome (axis lines, grid) and 1.5 px for data lines — matches the 0.5 px borders the rest of the host UI uses, so the chart doesn't feel chunkier than its neighbors.
- Add opacity="0.85" on data fills — softens the color slightly so it sits comfortably next to text without overwhelming it.

**Math hints for the less obvious shapes:**
- Donut arc length: circumference = 2 × π × r. To draw v% of the ring, set stroke-dasharray="{v×circumference/100} {circumference}" on the foreground circle, and transform="rotate(-90 cx cy)" so the arc starts at 12 o'clock instead of 3 o'clock.
- Bar widths in a viewBox="0 0 680 …": leave 40 px of margin on each side, giving a 600 px usable plot width.

---

## Hidden-container initialization

If an explicitly requested dashboard has tabs or hidden panels, do not initialize a chart in a zero-size container. Initialize it the first time the panel becomes visible, or resize an existing chart after showing it:

- Chart.js: `chart.resize()` on the existing instance.
- Plotly: `Plotly.Plots.resize(containerElement)`.
- Inline SVG with a fixed viewBox needs no library resize hook. Defer any custom measurement until its container is visible.

## Optional helper reference

These APIs are available when required by the task; their availability is not a reason to add buttons or panels. Use local JavaScript for filtering or changing chart views. Do not duplicate the tool's built-in controls.

| API | Behavior |
|-----|----------|
| `sendPrompt(text)` | Submits text as a new chat message. Use only from a deliberate user action in an explicitly requested conversational interface; never automatically during rendering. |
| `openLink(url)` | Opens a URL through the parent window; ordinary iframe links may be affected by sandbox restrictions. |
| `copyText(text)` | Copies text to the clipboard and shows localized feedback. |
| `toast(message, kind)` | Shows temporary feedback; kind is `success` (default), `info`, `warn`, or `error`. |
| `saveState(key, value)` / `loadState(key, fallback)` | Store and retrieve JSON-serializable state through parent localStorage. Use for existing filters or selections, not to justify adding controls. If storage is blocked, saving is a no-op and loading returns the fallback. |

In the saved format, state keys have the permanent visualization ID as a prefix, so charts in the same message do not collide. Restore needed state before drawing and save it when the user changes a control. This browser-local state is not a cross-device backup.

---

## Chart libraries and security

Only two libraries are installed, both served by the Open WebUI origin:

| Library | Local bundle | Available global |
|---------|--------------|------------------|
| **Chart.js** | `/static/chart.umd.min.js` | `window.Chart` |
| **Plotly** | `/static/plotly.umd.min.js` | `window.Plotly` |

The runtime loads both automatically. Do not add script imports for them,
request public CDNs, or assume any other libraries or plugins are installed.
Use Chart.js, Plotly, native SVG/Canvas, and plain JavaScript only.
Strict and balanced modes allow scripts only from the Open WebUI origin.

---

## Library init

Two patterns to follow when using a chart library:

### 1 · Wrap a Chart.js canvas in a fixed-height container

maintainAspectRatio: false makes Chart.js use the container's height.
If the canvas has no intrinsic height (e.g. inside a flex column without a height set), it collapses to zero and nothing draws:

<div style="position: relative; height: 260px;">
  <canvas id="chart"></canvas>
</div>
<script>
  new Chart(document.getElementById('chart').getContext('2d'), {
    type: 'bar',
    data: { /* … */ },
    options: { responsive: true, maintainAspectRatio: false, /* … */ }
  });
</script>

### 2 · Declare dependencies

Chart.js and Plotly load automatically; declare the used library names in
`data-iv-libraries` on each inline consumer. A consumer waits only for its
declared dependencies; a failure in the unused library does not block it.

<script data-iv-libraries="chartjs">/* uses Chart */</script>
<script data-iv-libraries="plotly">/* uses Plotly */</script>
