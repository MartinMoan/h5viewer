"""Non-modal top-level window showing an interactive Plotly.js chart for a
set of dataset columns. Frameless with the same hand-drawn chrome as the
main window (``SimpleTitleBar`` -- a lighter ``TitleBar`` variant with no
File/Settings/Help menu) via ``FramelessWindowMixin``, so it looks and
resizes/maximizes the same way the rest of the app does rather than
falling back to the OS's native window decorations. The chart's own
pan/zoom/reset/download controls are a small custom-styled toolbar built
directly into the page (see ``_SKELETON_HTML``) rather than Plotly's
stock modebar, whose icon set reads as dated next to the rest of this
app's hand-drawn icon language.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl, Qt
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import constants as c
from ..core.plotting import GraphConfig, build_plotly_spec
from ..theme import Palette, ThemeManager
from .frameless import FramelessWindowMixin
from .status_bar import StatusBar
from .title_bar import BAR_HEIGHT, SimpleTitleBar

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

# Plotly's own modebar is off (see _SKELETON_HTML's custom toolbar
# instead); scrollZoom on is what makes mouse-wheel zoom work at all --
# off by default in Plotly, and one of the more "modern web app" feeling
# interactions to have on for a chart like this.
_PLOTLY_CONFIG = {"displaylogo": False, "responsive": True, "displayModeBar": False, "scrollZoom": True}

# Inline SVG icons, hand-drawn to match the thin-stroke style of
# icons.py's Pillow-drawn icons (fit-to-view corner brackets / 4-way pan
# arrows / download tray) rather than reusing Plotly's own stock modebar
# glyphs.
_ICON_RESET = """
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>
</svg>
"""
_ICON_PAN = """
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 3v18M3 12h18M12 3l-3 3M12 3l3 3M12 21l-3-3M12 21l3-3M3 12l3-3M3 12l3 3M21 12l-3-3M21 12l-3 3"/>
</svg>
"""
_ICON_DOWNLOAD = """
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 3v12M7 10l5 5 5-5M4 19h16"/>
</svg>
"""

_SKELETON_HTML = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="plotly.min.js"></script>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; }}
  #root {{ position: relative; width: 100%; height: 100%; }}
  #chart {{ width: 100%; height: 100%; }}
  #toolbar {{
    position: absolute;
    top: 16px;
    right: 16px;
    z-index: 10;
    display: flex;
    gap: 8px;
  }}
  .tbtn {{
    width: 34px;
    height: 34px;
    border-radius: 17px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--btn-bg, #2A2A2A);
    border: 1px solid var(--btn-border, #3A3A3A);
    color: var(--btn-color, #E6E6E6);
    cursor: pointer;
    padding: 0;
  }}
  .tbtn:hover {{ background: var(--btn-hover, #272733); }}
  .tbtn.active {{ background: var(--btn-active, #4F46E5); border-color: var(--btn-active, #4F46E5); color: #FFFFFF; }}
  .tbtn svg {{ width: 16px; height: 16px; }}
</style>
</head>
<body>
<div id="root">
  <div id="chart"></div>
  <div id="toolbar">
    <button id="btn-reset" class="tbtn" title="Reset view" onclick="resetView()">{_ICON_RESET}</button>
    <button id="btn-pan" class="tbtn" title="Pan" onclick="togglePan()">{_ICON_PAN}</button>
    <button id="btn-download" class="tbtn" title="Download PNG" onclick="downloadChart()">{_ICON_DOWNLOAD}</button>
  </div>
</div>
<script>
  var panActive = false;
  function resetView() {{
    var gd = document.getElementById('chart');
    Plotly.relayout(gd, {{
      'xaxis.autorange': true, 'yaxis.autorange': true,
      'xaxis2.autorange': true, 'yaxis2.autorange': true
    }});
  }}
  function togglePan() {{
    panActive = !panActive;
    Plotly.relayout(document.getElementById('chart'), {{dragmode: panActive ? 'pan' : 'zoom'}});
    document.getElementById('btn-pan').classList.toggle('active', panActive);
  }}
  function downloadChart() {{
    Plotly.downloadImage(document.getElementById('chart'), {{format: 'png', filename: 'chart', scale: 2}});
  }}
</script>
</body>
</html>
"""


class GraphWindow(FramelessWindowMixin, QWidget):
    def __init__(
        self,
        theme: ThemeManager,
        labels: dict,
        config: GraphConfig,
        arrays: dict,
        truncated: bool,
        total_rows: int,
        title: str = "",
        file_path: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        window_title = f"Graph — {title}" if title else "Graph"
        self.setWindowTitle(window_title)
        self.resize(900, 640 + BAR_HEIGHT + 26)
        self._palette: Palette = theme.palette
        self._labels = labels
        self._config = config
        self._arrays = arrays
        self._loaded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = SimpleTitleBar(
            theme,
            window_title,
            on_minimize=self.showMinimized,
            on_toggle_maximize=self._toggle_maximize,
            on_close=self.close,
        )
        outer.addWidget(self.title_bar)

        self.warning_label = QLabel()
        self.warning_label.setTextFormat(Qt.TextFormat.RichText)
        self.warning_label.setVisible(truncated)
        if truncated:
            warn_color = c.WARN_COLOR_DARK if self._palette.dark else c.WARN_COLOR_LIGHT
            self.warning_label.setText(
                f'<span style="color:{warn_color};">Showing first {c.MAX_PLOT_ROWS:,} '
                f"of {total_rows:,} rows</span>"
            )
            self.warning_label.setContentsMargins(12, 6, 12, 6)
        outer.addWidget(self.warning_label)

        self.web_view = QWebEngineView()
        outer.addWidget(self.web_view, 1)
        self.web_view.loadFinished.connect(self._on_load_finished)
        self.web_view.setHtml(_SKELETON_HTML, baseUrl=QUrl.fromLocalFile(str(ASSETS_DIR) + "/"))
        # Plotly's downloadImage() (see the toolbar's downloadChart() JS
        # function) drives a real browser-style download, which
        # QWebEngineView surfaces as this signal rather than just saving
        # it -- has to be accepted explicitly or it's silently dropped.
        self.web_view.page().profile().downloadRequested.connect(self._on_download_requested)

        # Same info the main window's status bar shows for the open file
        # (StatusBar.set_path), plus which dataset within it this chart
        # came from -- reused directly rather than a bespoke label, so it
        # looks and behaves identically.
        self.status_bar = StatusBar(theme)
        self.status_bar.set_path(file_path)
        self.status_bar.set_context(title)
        outer.addWidget(self.status_bar)

        # Same reasoning as App: give the chrome widgets their own explicit
        # cursor so nothing shows a stray inherited one. Deliberately not
        # applied to web_view -- Plotly sets its own hover/crosshair/grab
        # cursors from JS, which a forced ArrowCursor here could clobber.
        for child in (self.title_bar, self.warning_label, self.status_bar):
            child.setCursor(Qt.CursorShape.ArrowCursor)

        self._init_frameless(BAR_HEIGHT)

        self._apply_palette(theme.palette)
        theme.register(self._apply_palette)

    def closeEvent(self, event) -> None:
        self._teardown_frameless()
        super().closeEvent(event)

    def _on_maximize_changed(self, maximized: bool) -> None:
        self.title_bar.set_maximized(maximized)

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = ok
        if ok:
            self._render()

    def _on_download_requested(self, download) -> None:
        directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        if not directory:
            directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        download.setDownloadDirectory(directory)
        download.setDownloadFileName(download.suggestedFileName() or "chart.png")
        download.isFinishedChanged.connect(lambda: self._on_download_finished(download))
        download.accept()

    def _on_download_finished(self, download) -> None:
        if download.isFinished():
            self.status_bar.set_message(f"Saved {download.downloadFileName()} to {download.downloadDirectory()}")

    def _render(self) -> None:
        if not self._loaded:
            return
        spec = build_plotly_spec(self._labels, self._config, self._arrays, self._palette)
        p = self._palette
        script = (
            f"Plotly.react('chart', {json.dumps(spec['data'])}, "
            f"{json.dumps(spec['layout'])}, {json.dumps(_PLOTLY_CONFIG)});"
            "var s = document.documentElement.style;"
            f"s.setProperty('--btn-bg', {json.dumps(p.button_bg)});"
            f"s.setProperty('--btn-border', {json.dumps(p.grid_line)});"
            f"s.setProperty('--btn-hover', {json.dumps(p.row_hover)});"
            f"s.setProperty('--btn-color', {json.dumps(p.text)});"
            f"s.setProperty('--btn-active', {json.dumps(p.accent)});"
        )
        self.web_view.page().runJavaScript(script)

    # -- theming ---------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        # header_bg, not base_bg: this is the window's own chrome color
        # (matches App's frame / the title/status bar), the same way
        # App itself is styled -- the actual plot area's background comes
        # from build_plotly_spec's paper_bgcolor (base_bg) instead.
        self.setStyleSheet(f"GraphWindow {{ background-color: {palette.header_bg}; }}")
        self._render()
