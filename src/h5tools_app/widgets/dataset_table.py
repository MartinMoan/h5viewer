"""Virtualized, continuously-scrollable table view for a single dataset.

``QTableView`` + a custom ``QAbstractTableModel`` already do the hard part
here: Qt only ever asks ``data()`` for cells that are actually on screen,
so we get virtualized scrolling over an arbitrarily large dataset for
free, and per-column background tinting is just a model role rather than
something we have to hand-paint. The only real work is wiring ``data()``
to a ``DatasetSource``: return a placeholder immediately if a row's block
hasn't loaded yet, kick off a background load, and let a small QTimer
poll the source and emit ``dataChanged`` for whatever finished since the
last tick.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import constants as c
from .. import icons
from ..core.dataset_source import DatasetSource
from ..core.h5_model import ColumnLayout, H5Model
from ..core.plotting import fetch_columns
from ..theme import Palette, ThemeManager
from .graph_config_dialog import GraphConfigDialog

POLL_MS = 40

# Ceiling for the double-click-to-fit column width -- deliberately much
# more generous than c.MAX_COL_WIDTH (which only bounds the crude,
# label-length-only initial guess in _apply_column_widths): fitting to
# content is an explicit, one-off user action, so a genuinely wide value
# should actually be shown in full rather than clipped back down to the
# same small default other columns start at.
_AUTOFIT_MAX_WIDTH = 640


def _selected_shade(hex_color: str, dark: bool) -> QColor:
    """A more saturated version of ``hex_color`` at the same hue, used to
    highlight a selected cell -- instead of Qt's default flat purple
    ``Highlight`` overwrite (see ``_NoHighlightDelegate``), which ignored
    each column's own tint entirely. Muted rather than vivid: "selected"
    only needs to read as *a shade of this column*, not as a hazard color
    (dark mode was previously pushed to a fairly bright/saturated
    mid-tone, e.g. #2f8f4f -- toned that down to a dimmer, still
    same-hue #2b5d3c; light mode dimming means less saturated/lighter
    rather than darker, so it reads muted rather than louder)."""
    base = QColor(hex_color)
    h, s, _l, _a = base.getHsl()
    if h < 0 or s < 10:
        # The default/unstyled column has no real hue to shade (its base
        # is a plain near-white/near-black gray) -- fall back to the
        # theme's ordinary selection tint rather than shading nothing.
        return QColor(c.SELECTION_DARK if dark else c.SELECTION_LIGHT)
    sat = min(max(s, 95 if dark else 70), 255)
    lightness = 68 if dark else 215
    return QColor.fromHsl(h, sat, lightness, 255)


class _NoHighlightDelegate(QStyledItemDelegate):
    """Paints every cell with its real BackgroundRole color, even when
    selected -- the model already swaps in a shade of the column's own
    tint for selected cells (see ``DatasetTableModel``/``_selected_shade``
    above), so this only needs to stop the style from clobbering that with
    its own flat ``Highlight`` palette color, which is what a selected
    ``QStyleOptionViewItem`` normally paints instead of BackgroundRole."""

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, index)


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, np.bytes_)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, (np.floating, float)):
        if np.isnan(value):
            return "NaN"
        return f"{value:.6g}"
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (np.bool_, bool)):
        return "True" if value else "False"
    if isinstance(value, np.ndarray):
        if value.size <= 6:
            return np.array2string(value, threshold=6)
        return f"<{'x'.join(str(d) for d in value.shape)} {value.dtype}>"
    return str(value)


class DatasetTableModel(QAbstractTableModel):
    def __init__(self, source: DatasetSource, palette: Palette, parent=None):
        super().__init__(parent)
        self.source = source
        self.layout_info: ColumnLayout = source.layout
        # Set once the owning QTableView has a model attached (see
        # DatasetTableView.load) -- QItemSelectionModel doesn't exist
        # until then, and only the view's selection tells data() which
        # cells to paint with the "selected" shade instead of the plain
        # column tint.
        self._selection_model: Optional[QItemSelectionModel] = None
        self._set_palette(palette)

    def attach_selection_model(self, selection_model: QItemSelectionModel) -> None:
        self._selection_model = selection_model

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.source.row_count

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.layout_info.n_columns

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()

        if role == Qt.ItemDataRole.BackgroundRole:
            if self._selection_model is not None and self._selection_model.isSelected(index):
                return self._selected_column_colors[col]
            return self._column_colors[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            arr, _missing = self.source.get_available(row, row + 1)
            return self._text_color if arr is not None else self._placeholder_color
        if role == Qt.ItemDataRole.DisplayRole:
            arr, _missing = self.source.get_available(row, row + 1)
            if arr is None:
                self.source.ensure_loaded(row, row + 1)
                return "···"
            return _format_cell(arr[0, col])
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.layout_info.labels[section]
        return str(section)

    def apply_palette(self, palette: Palette) -> None:
        self._set_palette(palette)
        rows, cols = self.rowCount(), self.columnCount()
        if rows and cols:
            top_left = self.index(0, 0)
            bottom_right = self.index(rows - 1, cols - 1)
            self.dataChanged.emit(
                top_left, bottom_right, [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole]
            )

    def poll_and_refresh(self) -> None:
        blocks = self.source.poll_updates()
        if not blocks:
            return
        block_size = self.source.block_size
        row_count = self.source.row_count
        col_count = self.layout_info.n_columns
        for block in blocks:
            start = block * block_size
            end = min(start + block_size, row_count)
            if start >= end:
                continue
            self.dataChanged.emit(self.index(start, 0), self.index(end - 1, col_count - 1))

    def _set_palette(self, palette: Palette) -> None:
        self._column_colors = [QColor(palette.column_color(i)) for i in range(self.layout_info.n_columns)]
        self._selected_column_colors = [
            _selected_shade(palette.column_color(i), palette.dark) for i in range(self.layout_info.n_columns)
        ]
        self._text_color = QColor(palette.text)
        self._placeholder_color = QColor(palette.subtext)


class _DatasetTable(QTableView):
    """Plain QTableView, except Shift+wheel scrolls horizontally instead of
    vertically -- the usual convention, and particularly useful here since
    a wide dataset can have far more columns than fit on screen at once."""

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Some platforms already convert Shift+wheel to a horizontal
            # delta (angleDelta().x()) before Qt sees it; where they don't,
            # fall back to treating the vertical delta as horizontal
            # ourselves.
            delta = event.angleDelta().x() or event.angleDelta().y()
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        super().wheelEvent(event)


class _HeaderInteractionFilter(QObject):
    """Installed on the horizontal header's viewport (not the header
    itself -- see below) to deselect a column on a second plain click,
    instead of the built-in header-click behavior (wired up automatically
    by QTableView, see the ExtendedSelection note above), which just
    re-selects the same already-selected column -- a click has no way to
    express "actually, never mind" there. Also auto-fits a column to its
    content when the border between two header sections is double-clicked
    -- the familiar spreadsheet gesture, which QHeaderView doesn't provide
    on its own for content that isn't sized purely from a delegate's size
    hint (this table's cell text comes from a lazily-loaded
    ``DatasetSource``, not a delegate).

    Swapping in a QHeaderView subclass via setHorizontalHeader() was tried
    first and rejected: QTableView only wires up its internal
    press-to-select-column handling for the *original* header instance it
    creates itself -- replacing it, even with a plain unmodified
    QHeaderView, silently breaks header-click selection entirely (click
    does nothing at all, confirmed independent of any subclass logic). An
    event filter on the existing header's viewport leaves that header
    instance untouched, so the built-in wiring stays intact; consuming the
    press (returning True) here stops it from ever reaching that handling
    for the deselect case, and returning False for every other case lets
    click-to-select, drag-to-multi-select, resize, move, and sort all keep
    working exactly as before.
    """

    def __init__(self, view: QTableView, on_autofit, parent=None):
        super().__init__(parent)
        self._view = view
        self._on_autofit = on_autofit

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            header = self._view.horizontalHeader()
            section = self._boundary_section(header, event.position().x())
            if section is not None:
                self._on_autofit(section)
                return True
            return False
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            header = self._view.horizontalHeader()
            logical = header.logicalIndexAt(event.position().toPoint())
            selection_model = self._view.selectionModel()
            if logical >= 0 and selection_model is not None:
                selected_cols = selection_model.selectedColumns()
                if len(selected_cols) == 1 and selected_cols[0].column() == logical:
                    selection_model.clearSelection()
                    return True
        return False

    @staticmethod
    def _boundary_section(header: QHeaderView, x: float) -> Optional[int]:
        """Returns the (logical) section whose right-hand border is under
        ``x``, or ``None`` if ``x`` isn't near a section boundary. Sampling
        just past each side of ``x`` and comparing the section reported at
        each point -- rather than computing a single section's edges
        directly -- keeps this correct under column reordering (sections
        are movable, see hheader.setSectionsMovable below), since
        logicalIndexAt already resolves visual position to logical index
        for us either way.
        """
        margin = 4
        left = header.logicalIndexAt(int(x) - margin)
        right = header.logicalIndexAt(int(x) + margin)
        if left >= 0 and right >= 0 and left != right:
            return left
        return None


class _NavPopover(QFrame):
    """Small floating panel holding the Top/End/jump-to-row controls,
    opened from a corner trigger button rather than living permanently in
    the toolbar -- keeps the dataset view down to just the table most of
    the time. A ``Qt.WindowType.Popup`` window, the same mechanism a combo
    box dropdown uses: it isn't a real (blocking) modal dialog, it just
    closes itself the moment you click anywhere outside it."""

    def __init__(self, theme: ThemeManager, owner: DatasetTableView):
        super().__init__(owner, Qt.WindowType.Popup)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.top_button = QPushButton("Top")
        self.top_button.clicked.connect(self._go_home)
        self.end_button = QPushButton("End")
        self.end_button.clicked.connect(self._go_end)
        layout.addWidget(self.top_button)
        layout.addWidget(self.end_button)
        layout.addSpacing(4)

        layout.addWidget(QLabel("Row"))
        self.goto_entry = QLineEdit()
        self.goto_entry.setFixedWidth(80)
        self.goto_entry.setPlaceholderText("#")
        self.goto_entry.returnPressed.connect(self._go_to)
        layout.addWidget(self.goto_entry)
        self.go_button = QPushButton("Go")
        self.go_button.clicked.connect(self._go_to)
        layout.addWidget(self.go_button)

        theme.register(self._apply_palette)

    def show_anchored_to(self, trigger: QWidget) -> None:
        self.goto_entry.clear()
        self.adjustSize()
        # Bottom-right corner of the popover lands exactly on the
        # trigger's own bottom-right corner, so it opens up-and-left from
        # the corner button rather than covering it or drifting away from
        # it, the way a tooltip anchors to whatever it's attached to.
        anchor = trigger.mapToGlobal(trigger.rect().bottomRight())
        self.move(anchor.x() - self.width(), anchor.y() - self.height())
        self.show()
        self.goto_entry.setFocus()

    def _go_home(self) -> None:
        self._owner._go_home()
        self.close()

    def _go_end(self) -> None:
        self._owner._go_end()
        self.close()

    def _go_to(self) -> None:
        self._owner._on_goto(self.goto_entry.text())
        self.close()

    def _apply_palette(self, palette: Palette) -> None:
        self.setStyleSheet(
            f"""
            _NavPopover {{
                background-color: {palette.base_bg};
                border: 1px solid {palette.grid_line};
                border-radius: 8px;
            }}
            QLabel {{ color: {palette.subtext}; }}
            QPushButton {{
                background-color: {palette.button_bg};
                color: {palette.text};
                border: none;
                border-radius: 4px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ background-color: {palette.row_hover}; }}
            QLineEdit {{
                background-color: {palette.base_bg};
                color: {palette.text};
                border: 1px solid {palette.grid_line};
                border-radius: 4px;
                padding: 2px 6px;
            }}
            """
        )


class DatasetTableView(QWidget):
    # Emits the "name · path · shape · dtype · N rows" summary line
    # (plus an HTML-colored truncation notice when applicable) every time
    # a dataset is loaded, and "" on clear() -- consumed by App to show it
    # in the status bar rather than reserving a title row of its own
    # space above the table. See load()/clear() below; this replaces what
    # used to be a title_label/subtitle_label/warning_label row here.
    context_changed = Signal(str)
    # Emitted when "Make Graph" is clicked without enough numeric columns
    # selected -- consumed by App to show it in the status bar as an error,
    # the same channel used for other user-facing error messages there.
    error_message = Signal(str)

    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._palette: Palette = theme.palette
        self._source: Optional[DatasetSource] = None
        self._table_model: Optional[DatasetTableModel] = None
        self._last_context: Optional[tuple] = None
        self._file_path: str = ""
        # Graph windows are independent, non-modal top-level widgets that
        # outlive whatever dataset happens to be loaded here -- kept alive
        # by this list (PySide6 would otherwise garbage-collect a shown
        # widget with no surviving Python reference almost immediately).
        self._graph_windows: list = []

        outer = QVBoxLayout(self)
        # Top margin matches HierarchyTree's own top layout margin (see
        # hierarchy_tree.py) so the table's content -- its column header
        # row -- lines up flush with the sidebar's top row, the same way
        # the two panes already share a left/right/bottom rhythm. This
        # pane used to reserve its own title/subtitle row above the table
        # (and, before that, a row of Top/End/Row/Go controls); both have
        # since moved out -- the controls to the corner popover, the
        # title/subtitle to the status bar (see context_changed above) --
        # so nothing but the table itself needs to live in this layout.
        outer.setContentsMargins(16, 10, 16, 16)
        outer.setSpacing(0)

        self.table = _DatasetTable()
        # ExtendedSelection + the default SelectItems behavior: a normal
        # click selects a single cell (shift/ctrl-click extend, as usual),
        # but clicking a horizontal header section is a distinct built-in
        # QTableView feature that selects the whole column regardless of
        # selectionBehavior -- Excel-like column selection falls out of
        # this for free, no extra wiring needed.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setShowGrid(True)
        vheader = self.table.verticalHeader()
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vheader.setDefaultSectionSize(c.ROW_HEIGHT)
        hheader = self.table.horizontalHeader()
        hheader.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hheader.setDefaultSectionSize(c.MIN_COL_WIDTH)
        # Purely visual reordering (Qt remaps visual <-> logical column
        # index internally) -- doesn't touch the model or the underlying
        # file, just the on-screen column order.
        hheader.setSectionsMovable(True)
        # A second plain click on an already-selected column deselects it,
        # and double-clicking a section border auto-fits that column to
        # its content -- see _HeaderInteractionFilter for why this is an
        # event filter on the header's viewport rather than a header
        # subclass.
        self._header_filter = _HeaderInteractionFilter(self.table, self._auto_fit_column, self)
        hheader.viewport().installEventFilter(self._header_filter)
        # Selected cells still get their BackgroundRole re-queried and
        # painted (see _NoHighlightDelegate) instead of the style's flat
        # Highlight-palette color, so they can be a shade of their own
        # column's tint (see DatasetTableModel/_selected_shade) rather
        # than one fixed purple regardless of column.
        self.table.setItemDelegate(_NoHighlightDelegate(self.table))

        self.empty_label = QLabel("Select a dataset from the tree to view its contents")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.table)
        outer.addWidget(self.stack, 1)

        # Floating corner trigger for the Top/End/jump-to-row controls,
        # not part of any layout -- it's a raw child of this widget,
        # explicitly positioned/raised so it floats above the table
        # instead of taking up its own row of toolbar space.
        self.nav_trigger = QPushButton(self)
        self.nav_trigger.setFixedSize(34, 34)
        self.nav_trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_trigger.clicked.connect(self._toggle_nav_popover)
        self.nav_trigger.hide()  # only relevant once a dataset is loaded
        self.nav_trigger.raise_()
        self._nav_popover = _NavPopover(theme, self)

        # Same floating-corner-button pattern as nav_trigger, positioned
        # just to its left -- appears only once 2+ columns are selected
        # (see _on_selection_changed, wired in load()).
        self.graph_trigger = QPushButton(self)
        self.graph_trigger.setFixedSize(34, 34)
        self.graph_trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        self.graph_trigger.setToolTip("Graph selected columns")
        self.graph_trigger.clicked.connect(self._on_make_graph)
        self.graph_trigger.hide()
        self.graph_trigger.raise_()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_MS)

        theme.register(self._apply_palette)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_nav_trigger()
        self._position_graph_trigger()

    def _position_nav_trigger(self) -> None:
        margin = 14
        area = self.stack.geometry()  # already in this widget's own coordinates
        x = area.right() - self.nav_trigger.width() - margin
        y = area.bottom() - self.nav_trigger.height() - margin
        self.nav_trigger.move(x, y)

    def _position_graph_trigger(self) -> None:
        gap = 8
        x = self.nav_trigger.x() - self.graph_trigger.width() - gap
        y = self.nav_trigger.y()
        self.graph_trigger.move(x, y)

    def _toggle_nav_popover(self) -> None:
        if self._nav_popover.isVisible():
            self._nav_popover.close()
        else:
            self._nav_popover.show_anchored_to(self.nav_trigger)

    # -- graphing ------------------------------------------------------

    def _on_selection_changed(self, *_args) -> None:
        # Cheap count check only -- numeric filtering happens at click
        # time in _on_make_graph, so selecting e.g. one numeric + one
        # string column still shows the trigger (clicking it is what
        # reports "not enough numeric columns", not the trigger's
        # visibility).
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return
        show = len(selection_model.selectedColumns()) >= 2
        self.graph_trigger.setVisible(show)
        if show:
            self.graph_trigger.raise_()
            self._position_graph_trigger()

    def _on_make_graph(self) -> None:
        if self._table_model is None or self._source is None:
            return
        layout = self._table_model.layout_info
        selected = [idx.column() for idx in self.table.selectionModel().selectedColumns()]
        numeric = [col for col in selected if layout.numeric_mask[col]]
        if len(numeric) < 2:
            self.error_message.emit("Select at least 2 numeric columns to graph.")
            return

        # Deferred, not a top-level import: QtWebEngineWidgets additionally
        # needs system Chromium runtime libraries (libnss3, libnspr4, ...)
        # that aren't pip-installable (see requirements.txt) -- importing
        # this eagerly at module load time meant the whole app failed to
        # even start on a system missing them, instead of only this one
        # feature being unavailable.
        try:
            from .graph_window import GraphWindow
        except ImportError as exc:
            self.error_message.emit(
                f"Graphing is unavailable: {exc}. See requirements.txt for the "
                "system packages QtWebEngine needs."
            )
            return

        labels = {col: layout.labels[col] for col in numeric}
        dialog = GraphConfigDialog(self._theme, labels, numeric, parent=self)
        config = dialog.get_config()
        if config is None:
            return

        col_indices = config.columns_used()
        arrays, truncated = fetch_columns(self._source.dataset, layout, col_indices)
        dataset_path = self._last_context[1] if self._last_context is not None else ""
        window = GraphWindow(
            self._theme,
            labels,
            config,
            arrays,
            truncated,
            layout.row_count,
            title=dataset_path,
            file_path=self._file_path,
        )
        self._graph_windows.append(window)

        def _forget(win=window) -> None:
            if win in self._graph_windows:
                self._graph_windows.remove(win)

        window.destroyed.connect(_forget)
        window.show()

    # -- public API --------------------------------------------------------

    def load(self, model: H5Model, path: str) -> None:
        self._teardown_source()
        dataset = model.get_dataset(path)
        layout = model.column_layout(path)
        node = model.node_info(path)

        self._source = DatasetSource(dataset, layout)
        self._table_model = DatasetTableModel(self._source, self._palette)
        self.table.setModel(self._table_model)
        # QItemSelectionModel is created fresh by setModel() above, so this
        # can only be wired up after it -- see DatasetTableModel.data().
        self._table_model.attach_selection_model(self.table.selectionModel())
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._apply_column_widths(layout)

        self._last_context = (node, path, layout)
        self._file_path = model.path
        self._emit_context()

        self.stack.setCurrentWidget(self.table)
        self.nav_trigger.show()
        self.nav_trigger.raise_()
        self._position_nav_trigger()
        self.graph_trigger.hide()  # fresh model has no selection yet
        self._position_graph_trigger()

    def clear(self) -> None:
        self._teardown_source()
        self._last_context = None
        self.context_changed.emit("")
        self.stack.setCurrentWidget(self.empty_label)
        self.nav_trigger.hide()
        self._nav_popover.close()
        self.graph_trigger.hide()

    def _emit_context(self) -> None:
        if self._last_context is None:
            return
        node, path, layout = self._last_context
        shape_txt = "scalar" if node.shape == () else "×".join(str(d) for d in node.shape)
        context = (
            f"{node.name}    ·    {path}    ·    shape {shape_txt}    ·    "
            f"{node.dtype}    ·    {layout.row_count:,} rows"
        )
        if layout.truncated:
            warn_color = c.WARN_COLOR_DARK if self._palette.dark else c.WARN_COLOR_LIGHT
            context += (
                f'    ·    <span style="color:{warn_color};">Showing first {layout.n_columns} '
                f"of {layout.total_columns:,} flattened columns</span>"
            )
        self.context_changed.emit(context)

    def _teardown_source(self) -> None:
        self.table.setModel(None)
        self._table_model = None
        if self._source is not None:
            self._source.close()
            self._source = None

    def _apply_column_widths(self, layout: ColumnLayout) -> None:
        header = self.table.horizontalHeader()
        for i, label in enumerate(layout.labels):
            width = min(c.MAX_COL_WIDTH, max(c.MIN_COL_WIDTH, len(label) * 9 + 30))
            header.resizeSection(i, width)

    def _auto_fit_column(self, logical: int) -> None:
        """Resizes column ``logical`` to fit its header label and its
        currently-loaded/visible cell values, plus a little padding --
        deliberately not ``QTableView.resizeColumnToContents``, which
        measures every row in the model: fine for an ordinary table, but
        this one is built to stay responsive over arbitrarily large
        datasets (see the module docstring), so it only measures what's
        actually already on screen/in memory instead of forcing a full
        column scan.
        """
        if self._table_model is None or self._source is None:
            return
        header = self.table.horizontalHeader()
        fm = QFontMetrics(self.table.font())
        layout = self._table_model.layout_info
        max_px = fm.horizontalAdvance(layout.labels[logical])

        top_row = self.table.rowAt(0)
        bottom_row = self.table.rowAt(max(0, self.table.viewport().height() - 1))
        if top_row < 0:
            top_row = 0
        if bottom_row < 0:
            bottom_row = min(top_row + 100, self._table_model.rowCount() - 1)
        if bottom_row >= top_row:
            arr, _missing = self._source.get_available(top_row, bottom_row + 1)
            if arr is not None:
                for row in arr[:, logical]:
                    max_px = max(max_px, fm.horizontalAdvance(_format_cell(row)))

        padding = 24
        width = min(_AUTOFIT_MAX_WIDTH, max(c.MIN_COL_WIDTH, max_px + padding))
        header.resizeSection(logical, width)

    # -- navigation ------------------------------------------------------

    def _go_home(self) -> None:
        if self._table_model is not None:
            self.table.verticalScrollBar().setValue(0)

    def _go_end(self) -> None:
        if self._table_model is not None:
            self.table.verticalScrollBar().setValue(self.table.verticalScrollBar().maximum())

    def _on_goto(self, text: str) -> None:
        if self._table_model is None:
            return
        text = text.strip()
        if not text.isdigit():
            return
        row = max(0, min(int(text), self._table_model.rowCount() - 1))
        self.table.verticalScrollBar().setValue(row)

    def _poll(self) -> None:
        if self._table_model is not None:
            self._table_model.poll_and_refresh()

    # -- theming -----------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        if self._table_model is not None:
            self._table_model.apply_palette(palette)
        self._emit_context()  # re-embeds the truncation-warning color for the new palette

        self.empty_label.setStyleSheet(f"color: {palette.subtext}; font-size: 12pt;")
        self.table.setStyleSheet(
            f"""
            QTableView {{
                background-color: {palette.body_bg};
                gridline-color: {palette.grid_line};
                border: none;
                /* Cell fills no longer come from QPalette::Highlight --
                   _NoHighlightDelegate strips the selected state before
                   painting, so selected cells use the per-column shade
                   from DatasetTableModel instead. These two still set
                   the Highlight/HighlightedText roles Fusion falls back
                   on elsewhere (e.g. text-selection highlighting inside
                   an editable field), so they stay, just no longer doing
                   the job they used to do here. */
                selection-background-color: {palette.selection};
                selection-color: {palette.text};
            }}
            QHeaderView::section {{
                background-color: {palette.header_bg};
                color: {palette.subtext};
                border: none;
                padding: 4px 8px;
            }}
            QHeaderView::section:horizontal {{
                border-bottom: 2px solid {palette.accent};
            }}
            QHeaderView::section:vertical {{
                border-bottom: 1px solid {palette.grid_line};
            }}
            """
        )
        self.nav_trigger.setIcon(icons.icon(icons.NAVIGATE, palette.text, 16))
        self.nav_trigger.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {palette.button_bg};
                border: 1px solid {palette.grid_line};
                border-radius: 17px;
            }}
            QPushButton:hover {{ background-color: {palette.row_hover}; }}
            """
        )
        self.graph_trigger.setIcon(icons.icon(icons.CHART, palette.text, 16))
        self.graph_trigger.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {palette.button_bg};
                border: 1px solid {palette.grid_line};
                border-radius: 17px;
            }}
            QPushButton:hover {{ background-color: {palette.row_hover}; }}
            """
        )
