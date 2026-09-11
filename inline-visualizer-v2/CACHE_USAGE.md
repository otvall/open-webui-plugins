## Using cached data

`visualize()` accepts an optional `cache_id`. When a valid `cache_id` is supplied, the selected cached tool result is automatically injected into the iframe and is available to generated JavaScript through:

```javascript
const data = window.getCachedData();
```

Follow these rules:

- Call `getCachedData()` only inside an executable `<script>` in the visualization.
- The value is already parsed. Never call `JSON.parse()` on it.
- Do not copy or recreate the dataset in HTML, JavaScript, or the `visualize()` call.
- Treat the returned value as the producer tool’s original result. Validate its type and structure before using it.
- Show a clear message inside the visualization if the dataset is empty or has an unexpected structure.
- One `visualize()` call accepts one `cache_id`.
- Use the `cache_id` exactly as provided. Never invent or modify it.
- If `visualize()` reports that the cache entry was not found, do not emit the `@@@VIZ-START` block because no iframe was mounted. Obtain a fresh `cache_id` or continue without cached data when appropriate.
- When no `cache_id` is supplied, `getCachedData()` is not available and must not be called.

Example:

```html
<div id="chart"></div>

<script>
  const data = window.getCachedData();
  const root = document.getElementById("chart");

  if (!Array.isArray(data) || data.length === 0) {
    root.textContent = "No data available for visualization.";
  } else {
    // Build the visualization from data here.
  }
</script>
```

This skill only explains how `visualize()` consumes an existing cache reference. Creation and lifetime of `cache_id` values are handled by the separate `tool-call-cache` skill.

Call visualize(title="…", cache_id="…" if cached data is available)

- When `cache_id` is supplied: the `getCachedData()` bridge