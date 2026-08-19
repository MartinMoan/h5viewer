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
from PySide6.QtGui import QColor
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
from ..theme import Palette, ThemeManager

POLL_MS = 40


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


class _ColumnToggleFilter(QObject):
    """Installed on the horizontal header's viewport (not the header
    itself -- see below) to deselect a column on a second plain click,
    instead of the built-in header-click behavior (wired up automatically
    by QTableView, see the ExtendedSelection note above), which just
    re-selects the same already-selected column -- a click has no way to
    express "actually, never mind" there.

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

    def __init__(self, view: QTableView, parent=None):
        super().__init__(parent)
        self._view = view

    def eventFilter(self, obj, event) -> bool:
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

    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._palette: Palette = theme.palette
        self._source: Optional[DatasetSource] = None
        self._table_model: Optional[DatasetTableModel] = None
        self._last_context: Optional[tuple] = None

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
        # A second plain click on an already-selected column deselects it
        # -- see _ColumnToggleFilter for why this is an event filter on
        # the header's viewport rather than a header subclass.
        self._column_toggle_filter = _ColumnToggleFilter(self.table, self)
        hheader.viewport().installEventFilter(self._column_toggle_filter)
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

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_MS)

        theme.register(self._apply_palette)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_nav_trigger()

    def _position_nav_trigger(self) -> None:
        margin = 14
        area = self.stack.geometry()  # already in this widget's own coordinates
        x = area.right() - self.nav_trigger.width() - margin
        y = area.bottom() - self.nav_trigger.height() - margin
        self.nav_trigger.move(x, y)

    def _toggle_nav_popover(self) -> None:
        if self._nav_popover.isVisible():
            self._nav_popover.close()
        else:
            self._nav_popover.show_anchored_to(self.nav_trigger)

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
        self._apply_column_widths(layout)

        self._last_context = (node, path, layout)
        self._emit_context()

        self.stack.setCurrentWidget(self.table)
        self.nav_trigger.show()
        self.nav_trigger.raise_()
        self._position_nav_trigger()

    def clear(self) -> None:
        self._teardown_source()
        self._last_context = None
        self.context_changed.emit("")
        self.stack.setCurrentWidget(self.empty_label)
        self.nav_trigger.hide()
        self._nav_popover.close()

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
            warn_color = "#E0A93B" if self._palette.dark else "#8A5A00"
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
                border-bottom: 2px solid {palette.accent};
                padding: 4px 8px;
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
