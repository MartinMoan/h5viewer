"""Modal dialog for configuring a graph from the dataset table's currently
selected (numeric) columns: pick one column as the X axis, and a chart
type -- plus, for non-Histogram series, which Y axis to plot against --
for every other column.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.plotting import ChartType, GraphConfig, SeriesSpec
from ..theme import Palette, ThemeManager
from .frameless import FramelessWindowMixin
from .title_bar import BAR_HEIGHT, SimpleTitleBar

_CHART_TYPE_LABELS = {
    ChartType.LINE: "Line",
    ChartType.SCATTER: "Scatter",
    ChartType.BAR: "Bar",
    ChartType.HISTOGRAM: "Histogram",
}


class GraphConfigDialog(FramelessWindowMixin, QDialog):
    def __init__(self, theme: ThemeManager, labels: dict, numeric_col_indices: list, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Make Graph")
        self.resize(480, 420 + BAR_HEIGHT)
        self._palette: Palette = theme.palette
        self._labels = labels
        self._col_indices = list(numeric_col_indices)
        self._series_combos: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = SimpleTitleBar(
            theme,
            "Make Graph",
            on_minimize=self.showMinimized,
            on_toggle_maximize=self._toggle_maximize,
            on_close=self.reject,
        )
        outer.addWidget(self.title_bar)

        content = QWidget()
        outer.addWidget(content, 1)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(18, 18, 18, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel("X axis"))
        self._x_group = QButtonGroup(self)
        self._x_radios: dict = {}
        x_col_widget = QWidget()
        x_col_layout = QVBoxLayout(x_col_widget)
        x_col_layout.setContentsMargins(0, 0, 0, 0)
        x_col_layout.setSpacing(2)
        for col in self._col_indices:
            radio = QRadioButton(self._labels[col])
            self._x_group.addButton(radio)
            self._x_radios[col] = radio
            x_col_layout.addWidget(radio)
        outer.addWidget(x_col_widget)
        self._x_radios[self._col_indices[0]].setChecked(True)
        self._x_group.buttonToggled.connect(self._on_x_changed)

        outer.addWidget(QLabel("Series"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._series_body = QWidget()
        self._series_layout = QVBoxLayout(self._series_body)
        self._series_layout.setContentsMargins(2, 2, 2, 2)
        self._series_layout.setSpacing(4)
        self._series_layout.addStretch(1)
        self.scroll.setWidget(self._series_body)
        outer.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        self.plot_button = QPushButton("Plot")
        self.plot_button.setDefault(True)
        self.plot_button.clicked.connect(self.accept)
        footer.addWidget(self.plot_button)
        outer.addLayout(footer)

        self.title_bar.setCursor(Qt.CursorShape.ArrowCursor)
        self._init_frameless(BAR_HEIGHT)

        self._rebuild_series_rows()
        self._apply_palette(theme.palette)
        theme.register(self._apply_palette)

    def get_config(self) -> Optional[GraphConfig]:
        if self.exec() == QDialog.DialogCode.Accepted:
            series = {
                col: SeriesSpec(chart_type=ChartType(type_combo.currentData()), axis=axis_combo.currentData())
                for col, (type_combo, axis_combo) in self._series_combos.items()
            }
            return GraphConfig(x_column=self._current_x_column(), series=series)
        return None

    def closeEvent(self, event) -> None:
        self._teardown_frameless()
        super().closeEvent(event)

    def _on_maximize_changed(self, maximized: bool) -> None:
        self.title_bar.set_maximized(maximized)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # QPushButton.setDefault() only controls Enter-activation, not
        # keyboard focus -- see FileOpenDialog for the same note.
        self.plot_button.setFocus()

    # -- internal ----------------------------------------------------------

    def _current_x_column(self) -> int:
        for col, radio in self._x_radios.items():
            if radio.isChecked():
                return col
        return self._col_indices[0]

    def _on_x_changed(self, button: QAbstractButton, checked: bool) -> None:
        if checked:
            self._rebuild_series_rows()

    def _rebuild_series_rows(self) -> None:
        while self._series_layout.count() > 1:
            item = self._series_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._series_combos = {}

        x_col = self._current_x_column()
        i = 0
        for col in self._col_indices:
            if col == x_col:
                continue
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.addWidget(QLabel(self._labels[col]), 1)
            type_combo = QComboBox()
            for chart_type, text in _CHART_TYPE_LABELS.items():
                type_combo.addItem(text, chart_type.value)
            row_layout.addWidget(type_combo)
            # Which Y axis this series plots against -- lets e.g.
            # temperature and pressure share an X column without one
            # flattening into a barely-visible line next to the other's
            # scale (see core/plotting.py's SeriesSpec/build_plotly_spec).
            # Meaningless for a Histogram series (it always gets its own
            # separate Count subplot), so disabled rather than removed --
            # switching chart type back re-enables it with its choice
            # intact, instead of resetting to "Left" every time.
            axis_combo = QComboBox()
            axis_combo.addItem("Left axis", "left")
            axis_combo.addItem("Right axis", "right")
            row_layout.addWidget(axis_combo)
            type_combo.currentIndexChanged.connect(
                lambda _idx, tc=type_combo, ac=axis_combo: ac.setEnabled(
                    ChartType(tc.currentData()) != ChartType.HISTOGRAM
                )
            )
            self._series_layout.insertWidget(i, row)
            self._series_combos[col] = (type_combo, axis_combo)
            i += 1

    # -- theming -------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"""
            GraphConfigDialog {{ background-color: {palette.window_bg}; color: {palette.text}; }}
            QLabel {{ color: {palette.text}; }}
            QRadioButton {{ color: {palette.text}; padding: 2px; }}
            QComboBox {{
                background-color: {palette.base_bg};
                border: 1px solid {palette.grid_line};
                border-radius: 6px;
                padding: 4px 8px;
                min-width: 110px;
            }}
            QPushButton {{
                background-color: {palette.button_bg};
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }}
            QPushButton:hover {{ background-color: {palette.row_hover}; }}
            QPushButton:default {{ background-color: {palette.accent}; color: white; }}
            QScrollArea {{ border: none; background-color: {palette.window_bg}; }}
            """
        )
