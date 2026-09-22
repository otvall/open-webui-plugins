---
status: accepted
---

# Generate completed visualizations inside the tool

`visualize` accepts a source tool call ID and title, validates the source result, and publishes a loading placeholder before making a nonstreaming model call with the full current conversation and the selected result. It then replaces that placeholder with a completed HTML/SVG embed, or an error. Generating HTML as a tool argument would delay both source validation and the loading placeholder until after the model had finished writing the HTML. The nested call uses the chat model by default, with an optional model override; context is never silently shortened. This adds a second model request and its latency and token cost.

The completed embed continues to use `getToolData()` to read the source result from the saved chat. This keeps the current data contract and same-origin requirement, including the existing limitation of standalone HTML downloads. Live completion keeps the ready toast and optional chime; reopening a saved chat stays silent.
