"""Modal dialog for configuring a graph from the dataset table's currently
selected (numeric) columns.

Two modes, switched via the top tab strip, sharing the same title
bar/footer chrome:

* "Chart" -- pick one column as the X axis, and for every other column a
  chart type (Line/Area/Scatter/Bar/Histogram/Box/Violin), which Y axis
  to plot against (for non-distribution types), and -- for Scatter --
  optional color-by/size-by bubble-chart column mapping. Plus a
  dialog-level "Axes" section for Bar grouping/stacking and per-axis
  log-scale toggles.
* "Map" -- pick a Latitude and a Longitude column (instead of X/series),
  optional color-by/size-by mapping, a "connect points" toggle for
  drawing a track/path, and an optional offline basemap file.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.plotting import ChartType, GraphConfig, MapConfig, SeriesSpec
from ..theme import Palette, ThemeManager
from .frameless import FramelessWindowMixin
from .title_bar import BAR_HEIGHT, SimpleTitleBar

_CHART_TYPE_LABELS = {
    ChartType.LINE: "Line",
    ChartType.AREA: "Area",
    ChartType.SCATTER: "Scatter",
    ChartType.BAR: "Bar",
    ChartType.HISTOGRAM: "Histogram",
    ChartType.BOX: "Box",
    ChartType.VIOLIN: "Violin",
}

# Distribution-summary chart types -- own subplot, ignore the shared X
# column and the left/right axis choice (see core/plotting.py). Kept as
# a small local duplicate of plotting._DISTRIBUTION_TYPES rather than
# importing that private name across module boundaries.
_DISTRIBUTION_TYPES = (ChartType.HISTOGRAM, ChartType.BOX, ChartType.VIOLIN)

_BASEMAP_FILE_FILTER = (
    "Basemap files (*.geojson *.json *.png *.jpg *.jpeg *.tif *.tiff);;All files (*)"
)


class GraphConfigDialog(FramelessWindowMixin, QDialog):
    def __init__(self, theme: ThemeManager, labels: dict, numeric_col_indices: list, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Make Graph")
        self.resize(560, 620 + BAR_HEIGHT)
        self._palette: Palette = theme.palette
        self._labels = labels
        self._col_indices = list(numeric_col_indices)
        self._series_combos: dict = {}
        self._basemap_path: Optional[str] = None

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

        self.mode_tabs = QTabWidget()
        self.mode_tabs.addTab(self._build_chart_tab(), "Chart")
        self.mode_tabs.addTab(self._build_map_tab(), "Map")
        outer.addWidget(self.mode_tabs, 1)

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

    def get_config(self):
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        if self.mode_tabs.currentIndex() == 0:
            series = {}
            for col, (type_combo, axis_combo, color_combo, size_combo) in self._series_combos.items():
                chart_type = ChartType(type_combo.currentData())
                is_scatter = chart_type == ChartType.SCATTER
                series[col] = SeriesSpec(
                    chart_type=chart_type,
                    axis=axis_combo.currentData(),
                    color_by=color_combo.currentData() if is_scatter else None,
                    size_by=size_combo.currentData() if is_scatter else None,
                )
            return GraphConfig(
                x_column=self._current_checked(self._x_radios),
                series=series,
                bar_mode=self.bar_mode_combo.currentData(),
                log_x=self.log_x_check.isChecked(),
                log_y_left=self.log_y_left_check.isChecked(),
                log_y_right=self.log_y_right_check.isChecked(),
            )
        return MapConfig(
            lat_column=self._current_checked(self._lat_radios),
            lon_column=self._current_checked(self._lon_radios),
            color_by=self._map_color_combo.currentData(),
            size_by=self._map_size_combo.currentData(),
            connect_points=self._connect_points_check.isChecked(),
            basemap_path=self._basemap_path,
            lat_lon_units=self._map_units_combo.currentData(),
        )

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

    # -- Chart tab -----------------------------------------------------

    def _build_chart_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(2, 8, 2, 2)
        layout.setSpacing(10)

        layout.addWidget(QLabel("X axis"))
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
        layout.addWidget(x_col_widget)
        self._x_radios[self._col_indices[0]].setChecked(True)
        self._x_group.buttonToggled.connect(self._on_x_changed)

        layout.addWidget(QLabel("Series"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._series_body = QWidget()
        self._series_layout = QVBoxLayout(self._series_body)
        self._series_layout.setContentsMargins(2, 2, 2, 2)
        self._series_layout.setSpacing(4)
        self._series_layout.addStretch(1)
        self.scroll.setWidget(self._series_body)
        layout.addWidget(self.scroll, 1)

        layout.addWidget(QLabel("Axes"))
        axes_widget = QWidget()
        axes_layout = QVBoxLayout(axes_widget)
        axes_layout.setContentsMargins(0, 0, 0, 0)
        axes_layout.setSpacing(4)

        bar_row = QHBoxLayout()
        bar_row.addWidget(QLabel("Bar mode (applies when 2+ Bar series exist)"))
        self.bar_mode_combo = QComboBox()
        self.bar_mode_combo.addItem("Grouped", "group")
        self.bar_mode_combo.addItem("Stacked", "stack")
        bar_row.addWidget(self.bar_mode_combo)
        bar_row.addStretch(1)
        axes_layout.addLayout(bar_row)

        log_row = QHBoxLayout()
        self.log_x_check = QCheckBox("Log X")
        self.log_y_left_check = QCheckBox("Log left Y")
        self.log_y_right_check = QCheckBox("Log right Y")
        for cb in (self.log_x_check, self.log_y_left_check, self.log_y_right_check):
            log_row.addWidget(cb)
        log_row.addStretch(1)
        axes_layout.addLayout(log_row)
        layout.addWidget(axes_widget)

        return tab

    def _current_checked(self, radios: dict) -> int:
        for col, radio in radios.items():
            if radio.isChecked():
                return col
        return self._col_indices[0]

    def _on_x_changed(self, button: QAbstractButton, checked: bool) -> None:
        if checked:
            self._rebuild_series_rows()

    def _make_column_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.addItem("None", None)
        for col in self._col_indices:
            combo.addItem(self._labels[col], col)
        return combo

    def _rebuild_series_rows(self) -> None:
        while self._series_layout.count() > 1:
            item = self._series_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._series_combos = {}

        x_col = self._current_checked(self._x_radios)
        i = 0
        for col in self._col_indices:
            if col == x_col:
                continue
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(3)

            top_row = QHBoxLayout()
            top_row.addWidget(QLabel(self._labels[col]), 1)
            type_combo = QComboBox()
            for chart_type, text in _CHART_TYPE_LABELS.items():
                type_combo.addItem(text, chart_type.value)
            top_row.addWidget(type_combo)
            # Which Y axis this series plots against -- lets e.g.
            # temperature and pressure share an X column without one
            # flattening into a barely-visible line next to the other's
            # scale (see core/plotting.py's SeriesSpec/build_plotly_spec).
            # Meaningless for a distribution-summary series (Histogram/
            # Box/Violin -- each always gets its own separate subplot),
            # so disabled rather than removed -- switching chart type
            # back re-enables it with its choice intact.
            axis_combo = QComboBox()
            axis_combo.addItem("Left axis", "left")
            axis_combo.addItem("Right axis", "right")
            top_row.addWidget(axis_combo)
            row_layout.addLayout(top_row)

            # Scatter-only bubble-chart mapping -- kept in its own row,
            # hidden entirely for every other chart type, so the common
            # case (Line/Bar/Histogram/...) stays exactly as uncluttered
            # as before this was added.
            bubble_widget = QWidget()
            bubble_row = QHBoxLayout(bubble_widget)
            bubble_row.setContentsMargins(0, 0, 0, 0)
            bubble_row.addWidget(QLabel("Color by"))
            color_combo = self._make_column_combo()
            bubble_row.addWidget(color_combo, 1)
            bubble_row.addWidget(QLabel("Size by"))
            size_combo = self._make_column_combo()
            bubble_row.addWidget(size_combo, 1)
            row_layout.addWidget(bubble_widget)

            def _on_type_changed(_idx, tc=type_combo, ac=axis_combo, bw=bubble_widget) -> None:
                chart_type = ChartType(tc.currentData())
                ac.setEnabled(chart_type not in _DISTRIBUTION_TYPES)
                bw.setVisible(chart_type == ChartType.SCATTER)

            type_combo.currentIndexChanged.connect(_on_type_changed)
            _on_type_changed(0)

            self._series_layout.insertWidget(i, row)
            self._series_combos[col] = (type_combo, axis_combo, color_combo, size_combo)
            i += 1

    # -- Map tab ---------------------------------------------------------

    def _build_map_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(2, 8, 2, 2)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Latitude column"))
        self._lat_group = QButtonGroup(self)
        self._lat_radios: dict = {}
        lat_widget = self._make_radio_column(self._lat_group, self._lat_radios)
        layout.addWidget(lat_widget)

        layout.addWidget(QLabel("Longitude column"))
        self._lon_group = QButtonGroup(self)
        self._lon_radios: dict = {}
        lon_widget = self._make_radio_column(self._lon_group, self._lon_radios)
        layout.addWidget(lon_widget)

        # Default to two different columns when there are at least two
        # candidates, so Lat/Lon don't silently start out pointing at the
        # same column.
        self._lat_radios[self._col_indices[0]].setChecked(True)
        default_lon = self._col_indices[1] if len(self._col_indices) > 1 else self._col_indices[0]
        self._lon_radios[default_lon].setChecked(True)

        units_row = QHBoxLayout()
        units_row.addWidget(QLabel("Units"))
        self._map_units_combo = QComboBox()
        self._map_units_combo.addItem("Degrees", "degrees")
        self._map_units_combo.addItem("Radians", "radians")
        units_row.addWidget(self._map_units_combo)
        units_row.addStretch(1)
        layout.addLayout(units_row)

        bubble_row = QHBoxLayout()
        bubble_row.addWidget(QLabel("Color by"))
        self._map_color_combo = self._make_column_combo()
        bubble_row.addWidget(self._map_color_combo, 1)
        bubble_row.addWidget(QLabel("Size by"))
        self._map_size_combo = self._make_column_combo()
        bubble_row.addWidget(self._map_size_combo, 1)
        layout.addLayout(bubble_row)

        self._connect_points_check = QCheckBox("Connect points (draw a path in row order)")
        layout.addWidget(self._connect_points_check)

        layout.addWidget(QLabel("Basemap"))
        basemap_row = QHBoxLayout()
        self._basemap_label = QLabel("None (offline outline map)")
        basemap_row.addWidget(self._basemap_label, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse_basemap)
        basemap_row.addWidget(browse_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear_basemap)
        basemap_row.addWidget(clear_btn)
        layout.addLayout(basemap_row)

        layout.addStretch(1)
        return tab

    def _make_radio_column(self, group: QButtonGroup, radios: dict) -> QWidget:
        widget = QWidget()
        col_layout = QVBoxLayout(widget)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(2)
        for col in self._col_indices:
            radio = QRadioButton(self._labels[col])
            group.addButton(radio)
            radios[col] = radio
            col_layout.addWidget(radio)
        return widget

    def _on_browse_basemap(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose a basemap file", "", _BASEMAP_FILE_FILTER)
        if path:
            self._basemap_path = path
            self._basemap_label.setText(path.rsplit("/", 1)[-1])

    def _on_clear_basemap(self) -> None:
        self._basemap_path = None
        self._basemap_label.setText("None (offline outline map)")

    # -- theming -------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"""
            GraphConfigDialog {{ background-color: {palette.window_bg}; color: {palette.text}; }}
            QLabel {{ color: {palette.text}; }}
            QCheckBox {{ color: {palette.text}; }}
            QRadioButton {{ color: {palette.text}; padding: 2px; }}
            QTabWidget::pane {{ border: 1px solid {palette.grid_line}; border-radius: 6px; top: -1px; }}
            QTabBar::tab {{
                background-color: {palette.header_bg};
                color: {palette.subtext};
                padding: 6px 16px;
                border: none;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                background-color: {palette.window_bg};
                color: {palette.text};
                border-bottom: 2px solid {palette.accent};
            }}
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
