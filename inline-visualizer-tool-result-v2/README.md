# Inline Visualizer — Tool Result Edition

Turn a JSON result from a Native tool call into an interactive chart or HTML/SVG view in Open WebUI. The tool generates the visual in a separate, nonstreaming model call. The chat shows a loading placeholder, then a completed visualization or an error. The assistant never prints the visual's source in its answer.

## How it works

1. A JSON-producing tool finishes. The assistant reads `view_skill("visualize")` and calls `visualize(source_tool_call_id="<exact call ID>", title="…")`.
2. `visualize` checks the call ID and result before spending tokens on HTML generation. It publishes a loading embed in the current message.
3. The tool sends the available conversation and selected result to the chat model, or to the model configured in `generation_model_id`. It requests one complete HTML/SVG fragment with `stream=False`.
4. The tool replaces the loading embed with a complete iframe. The iframe loads the original JSON through `getToolData()` and displays the chart after its initial scripts and data have loaded. An error replaces the loader if generation or browser rendering fails.
5. The assistant briefly describes the finished chart in ordinary text. It does not emit HTML or visualization markers.

The result is selected by exact tool call ID. The model sees the selected JSON while designing the chart, but the data is not copied into generated HTML. The iframe reads it from the saved chat when it mounts.

### Example request

> Run the SQL query for daily revenue and plot its result as a line chart.

The assistant calls the SQL tool, then `visualize(source_tool_call_id="<SQL call ID>", title="Daily revenue")`. The tool handles generation and display. For another chart, call `visualize` again with the relevant result ID. Calls should be sequential so each loader and final embed has its own place in the message.

## Setup

Requirements: Open WebUI 0.11.1, a saved chat, Native function calling, a JSON-producing tool, a server-side model available to the user, and **Allow iframe same origin** enabled. The current `getToolData()` reads the saved chat from the iframe, so the same-origin setting remains required even though HTML generation is no longer streamed.

1. Copy [tool.py](tool.py) into **Workspace → Tools → Create New**, then save it.
2. Import [SKILL.md](SKILL.md) as a skill named `visualize` in **Workspace → Knowledge / Skills**.
3. Attach the tool and skill to the model under **Admin Panel → Settings → Models**. Set **Function Calling** to **Native**.
4. In **User Settings → Interface**, enable **Allow iframe same origin**.

The tool call needs the exact ID of a completed JSON result. It checks current conversation messages, the active response stream, and saved message output before it starts the internal model call. If the ID is absent or the result is not a JSON object or array, it returns an error without generating HTML.

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
| `strict` | Blocked | Blocked | Three allowlisted CDNs |
| `balanced` | Blocked | Allowed | Three allowlisted CDNs |
| `offline` | Blocked | Blocked | Same-origin self-hosted files |
| `none` | Allowed | Allowed | Unrestricted by this CSP |

The iframe calls `parent.fetch` to read the chat's JSON result; the iframe's `connect-src` policy does not govern that parent request. With **Allow iframe same origin**, generated JavaScript can also reach the parent Open WebUI page. This is a platform permission, and the tool cannot narrow it through CSP. Use a trusted model and review this setting for your deployment.

In `offline` mode, inline SVG/HTML works without library files. To use Chart.js, D3, or another library, host its script under your Open WebUI `/static/` path and reference that path in the skill instructions. No external CDN is allowed in that mode.

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

- **No placeholder appears:** save the chat, enable Native function calling, and check that the selected result is a completed JSON tool output with the exact call ID.
- **The loader turns into an error:** the internal model may have timed out, returned incomplete HTML, or lacked enough context. The tool does not retry automatically.
- **The final iframe reports unavailable data:** enable **Allow iframe same origin**, keep the chat saved, and confirm the source tool result remains in chat history.
- **A chart is blank:** give Chart.js canvases an explicitly sized container and use `maintainAspectRatio: false`. Put external library scripts before the script that uses them.
- **A library fails in `offline` mode:** serve it from `/static/` and update the URL in the skill.
- **No sound on an old chart:** the chime is intentionally limited to a live placeholder-to-ready transition. Disable it globally with the `chime` valve.

## Development

Run `pytest -q` for the tool contract, source-result bridge, embed lifecycle, and CSP checks. [CONTEXT.md](CONTEXT.md) defines the project language; [ADR 0001](docs/adr/0001-generate-visualization-inside-tool.md) records why HTML generation lives inside the tool.
