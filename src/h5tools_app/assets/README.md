# Vendored assets

`plotly.min.js` — Plotly.js v2.35.2, MIT licensed, fetched from
`https://cdn.plot.ly/plotly-2.35.2.min.js`. Vendored (not a pip package)
so the graph window (`widgets/graph_window.py`) can load it locally via
`QWebEngineView` without a network dependency or adding `plotly`/
`pandas`/`scipy` to `requirements.txt` — only the JS library is used, the
Plotly JSON spec is built directly in `core/plotting.py`.

To update: replace this file with a newer `https://cdn.plot.ly/plotly-<version>.min.js`
build and update the version note here.
