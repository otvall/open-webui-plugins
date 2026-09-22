# Inline Visualizer — Tool Result Edition

Turn a JSON result from a Native tool call into a single interactive chart with axes, labels, and a legend in Open WebUI. The tool generates the visual in a separate, nonstreaming model call. The chat shows a loading placeholder, then a completed visualization or an error. The assistant never prints the visual's source in its answer.

## How it works

1. A JSON-producing tool finishes. The assistant reads `view_skill("visualize")` and calls `visualize(source_tool_name="<exact tool name>", title="…")`.
2. `visualize` finds the named tool’s latest successfully completed JSON result in the current conversation branch before spending tokens on HTML generation. It publishes a loading embed in the current message.
3. The tool sends the available conversation and selected result to the chat model, or to the model configured in `generation_model_id`. It requests one complete HTML/SVG fragment with `stream=False`.
4. The tool replaces the loading embed with a complete iframe. The iframe loads the original JSON through `getToolData()` and displays the chart after its initial scripts and data have loaded. An error replaces the loader if generation or browser rendering fails.
5. The assistant briefly describes the finished chart in ordinary text. It does not emit HTML or visualization markers.

Each visualization contains one plotting area, which may show multiple curves or series. Hover tooltips, zoom, and legend toggles are allowed; dashboards, data tables, filters, and extra panels are excluded. The runtime retains its download controls and loading/error feedback.

The assistant supplies the exact callable tool name, not a call ID. The server selects the latest eligible result and pins its real call ID in the embed, so later calls do not change an existing chart. The model sees the selected JSON while designing the chart, but the data is not copied into generated HTML. The iframe reads it from the saved chat when it mounts.

### Example request

> Run the SQL query for daily revenue and plot its result as a line chart.

If the SQL tool is named `run_sql`, the assistant calls it and then calls `visualize(source_tool_name="run_sql", title="Daily revenue")`. For a chart of a different query, execute that query last before calling `visualize` again. Run each data-tool → visualize pair sequentially. No result-list tool or model-generated call ID is needed.

## Setup

Requirements: Open WebUI 0.11.1, a saved chat, Native function calling, a JSON-producing tool, a server-side model available to the user, and **Allow iframe same origin** enabled. The current `getToolData()` reads the saved chat from the iframe, so the same-origin setting remains required even though HTML generation is no longer streamed.

1. Copy [tool.py](tool.py) into **Workspace → Tools → Create New**, then save it.
2. Import [SKILL.md](SKILL.md) as a skill named `visualize` in **Workspace → Knowledge / Skills**.
3. Attach the tool and skill to the model under **Admin Panel → Settings → Models**. Set **Function Calling** to **Native**.
4. In **User Settings → Interface**, enable **Allow iframe same origin**.
5. Serve the local library files from the Open WebUI origin at these exact paths:

   | Library | Path |
   | --- | --- |
   | Chart.js | `/static/chart.umd.min.js` |
   | Plotly | `/static/plotly.min.js` |
   | html2canvas (PNG export helper) | `/static/html2canvas.min.js` |

   These files are hosted separately and are not bundled in this repository. There is no CDN fallback.

The tool matches the callable name exactly against the current response stream, saved current-message output, and previous messages supplied for the current branch. Repeated snapshots of a call are deduplicated; live output takes precedence. Pending, failed, cancelled, and non-JSON results are skipped. When older tool messages lack a status, Open WebUI's JSON error markers identify failed results; an explicit completed status remains authoritative. If no completed JSON object or array exists for that tool name, it returns an error without generating HTML.

This changes the public argument from `source_tool_call_id` to `source_tool_name`; update both the imported tool and skill. Existing saved embeds retain their original runtime and call-ID binding. New embeds also record their containing message so data lookup follows that message’s branch.

## Tool settings

| Valve | Default | Purpose |
| --- | --- | --- |
| `security_level` | `strict` | Iframe CSP: `strict`, `balanced`, `offline`, or `none`. |
| `chime` | `true` | Soft sound when a live loading embed becomes ready. Reopened charts stay silent. |
| `generation_model_id` | empty | Override the chat model for the internal HTML generation call. Use a server-side model or Pipe. |
| `generation_max_tokens` | `12000` | Output token limit for the complete HTML fragment. |
| `generation_timeout_seconds` | `180` | Limit for the internal model call. |
| `generation_context_max_chars` | `1000000` | Reject larger serialized context instead of silently truncating it. |

The internal call consumes additional model tokens and may add latency. It receives the full context available to the tool, including the selected result. If the selected model cannot handle that context, generation fails visibly; the tool does not silently remove earlier messages or retry.

### Security levels

| Level | Runtime data fetch | External images | Script libraries |
| --- | --- | --- | --- |
| `strict` | Blocked | Blocked | Same-origin self-hosted files |
| `balanced` | Blocked | Allowed | Same-origin self-hosted files |
| `offline` | Blocked | Blocked | Same-origin self-hosted files |
| `none` | Allowed | Allowed | Unrestricted by this CSP |

The iframe calls `parent.fetch` to read the chat's JSON result; the iframe's `connect-src` policy does not govern that parent request. With **Allow iframe same origin**, generated JavaScript can also reach the parent Open WebUI page. This is a platform permission, and the tool cannot narrow it through CSP. Use a trusted model and review this setting for your deployment.

The renderer instructions permit only the local Chart.js or Plotly bundle for chart generation. PNG export may additionally load the local html2canvas helper. `strict`, `balanced`, and `offline` allow same-origin script files and block CDN scripts; `offline` now uses the same CSP as `strict`. The CSP restricts script origins, while the skill and internal prompt specify the permitted libraries. `none` still disables CSP explicitly.

## What the iframe provides

- Theme-aware CSS, nine color ramps, SVG classes, and default styles for plain HTML controls.
- `getToolData()` — Promise of a fresh parsed copy of the selected JSON result.
- `sendPrompt(text)`, `openLink(url)`, `copyText(text)`, and `toast(message, kind)` bridges.
- `saveState(key, value)` and `loadState(key, fallback)` for per-message interactive state.
- HTML, SVG, and PNG download controls; 48-language labels and ready feedback in the completed iframe.
- Resize reporting, a localized loader while browser data loads, and an error message if data or chart code fails.

The completed iframe stores its HTML in the chat embed. Old saved chats with the previous marker protocol retain their embedded runtime and should continue to reopen. New calls use the one-shot protocol.

### Download limitation

A downloaded HTML file includes the current DOM, but a data-driven script that calls `getToolData()` cannot reload JSON outside its saved Open WebUI chat. Use PNG or SVG for a portable snapshot. Making HTML exports fully standalone would require embedding the source data in the artifact, which this version does not do.

## Troubleshooting

- **No placeholder appears:** save the chat, enable Native function calling, and check that the named data tool has a completed JSON result. Pass its exact callable name, without invented prefixes or suffixes.
- **The loader turns into an error:** the internal model may have timed out, returned incomplete HTML, or lacked enough context. The tool does not retry automatically.
- **The final iframe reports unavailable data:** enable **Allow iframe same origin**, keep the chat saved, and confirm the source tool result remains in chat history.
- **A chart is blank:** give Chart.js canvases an explicitly sized container and use `maintainAspectRatio: false`. Put external library scripts before the script that uses them.
- **A library fails to load:** verify that the exact `/static/` paths listed above return JavaScript from your Open WebUI instance. A missing local file has no CDN fallback.
- **No sound on an old chart:** the chime is intentionally limited to a live placeholder-to-ready transition. Disable it globally with the `chime` valve.

## Development

Run `pytest -q` for the tool contract, source-result bridge, embed lifecycle, and CSP checks. [CONTEXT.md](CONTEXT.md) defines the project language; [ADR 0001](docs/adr/0001-generate-visualization-inside-tool.md) records why HTML generation lives inside the tool, and [ADR 0002](docs/adr/0002-select-latest-result-by-tool-name.md) records source selection by tool name.

Regression tests invoke `Tools.visualize()` with only external Open WebUI services replaced. Browser tests execute the emitted iframe in Chromium, with a simulated chat API and animation clock, and check that errors remain visible after the chart is ready. They use Chromium from `PATH` or the standard Playwright cache; set `IV_TEST_CHROMIUM` to select an executable explicitly. Without Chromium, the browser tests are skipped.
