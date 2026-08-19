"""Non-modal top-level window showing an interactive Plotly.js chart for a
set of dataset columns. Frameless with the same hand-drawn chrome as the
main window (``SimpleTitleBar`` -- a lighter ``TitleBar`` variant with no
File/Settings/Help menu) via ``FramelessWindowMixin``, so it looks and
resizes/maximizes the same way the rest of the app does rather than
falling back to the OS's native window decorations.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import constants as c
from ..core.plotting import GraphConfig, build_plotly_spec
from ..theme import Palette, ThemeManager
from .frameless import FramelessWindowMixin
from .title_bar import BAR_HEIGHT, SimpleTitleBar

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

_PLOTLY_CONFIG = {"displaylogo": False, "responsive": True}

_SKELETON_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="plotly.min.js"></script>
<style>
  html, body { margin: 0; padding: 0; height: 100%; }
  #chart { width: 100%; height: 100%; }
</style>
</head>
<body>
<div id="chart"></div>
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
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        window_title = f"Graph — {title}" if title else "Graph"
        self.setWindowTitle(window_title)
        self.resize(900, 640 + BAR_HEIGHT)
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

        # Same reasoning as App: give the chrome widgets their own explicit
        # cursor so nothing shows a stray inherited one. Deliberately not
        # applied to web_view -- Plotly sets its own hover/crosshair/grab
        # cursors from JS, which a forced ArrowCursor here could clobber.
        for child in (self.title_bar, self.warning_label):
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

    def _render(self) -> None:
        if not self._loaded:
            return
        spec = build_plotly_spec(self._labels, self._config, self._arrays, self._palette)
        script = (
            f"Plotly.react('chart', {json.dumps(spec['data'])}, "
            f"{json.dumps(spec['layout'])}, {json.dumps(_PLOTLY_CONFIG)});"
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
