"""Custom-styled "Open File" dialog.

A real ``QDialog`` -- properly parented, so it doesn't have the
window-manager stacking/focus quirks an unparented Tk ``Toplevel`` had --
but with its own row/list widgets instead of Qt's stock ``QFileDialog``
layout. Left as the stock dialog, it only picks up our ``QApplication``
palette (base colors); the app's QSS styling (rounded rows, accent icons,
hover states) lives on the main window's stylesheet, which doesn't
cascade across separate top-level widgets, so it would look like a plain
Fusion dialog rather than matching the rest of the app. Path completion
is hand-rolled (Tab fills in the first matching name in the current
directory) rather than Qt's ``QCompleter``+``QFileSystemModel`` -- that
combination turned out not to actually produce completions for an
absolute path prefix in practice (``completionCount()`` stayed 0 even
with a real directory and time for its async scan to finish), and this
same logic was already written and proven correct for an earlier
prototype of this dialog.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..core.h5_model import DATASET, GROUP
from ..theme import Palette, ThemeManager
from .group_panel import _ClickableRow

H5_SUFFIXES = {".h5", ".hdf5", ".he5"}
ICON_SIZE = 17


def _suggest_completion(typed: str) -> Optional[str]:
    """Return ``typed`` extended with the first matching folder/file name
    in whatever directory it currently points into, or None."""
    if not typed:
        return None
    ends_with_sep = typed.endswith(("/", "\\"))
    raw = Path(typed).expanduser()
    base_dir, prefix = (raw, "") if ends_with_sep else (raw.parent, raw.name)

    try:
        if not base_dir.is_dir():
            return None
        candidates = sorted(
            e.name for e in base_dir.iterdir()
            if e.name.lower().startswith(prefix.lower())
            and not e.name.startswith(".")
            and (e.is_dir() or e.suffix.lower() in H5_SUFFIXES)
        )
    except OSError:
        return None

    if not candidates or len(candidates[0]) <= len(prefix):
        return None

    best = candidates[0]
    sep = "\\" if ("\\" in typed and "/" not in typed) else "/"
    suggestion = typed + best if ends_with_sep else typed[: len(typed) - len(prefix)] + best
    if (base_dir / best).is_dir():
        suggestion += sep
    return suggestion


class _PathEdit(QLineEdit):
    """A QLineEdit that shows an inline "ghost" suggestion (the first
    matching name in the current directory, pre-selected) as you type,
    and fills it in fully on Tab -- like a shell or a native file picker.
    Keep typing to override the suggestion; it's just a selection, so the
    next keystroke replaces it like any other selected text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._deleting = False
        self.textEdited.connect(self._on_text_edited)

    def _on_text_edited(self, text: str) -> None:
        if self._deleting:
            # Backspace/Delete: never snap back to a longer suggestion.
            # Without this, deleting into a prefix that still matches an
            # existing name (e.g. "/home/user/projec" while "project"
            # exists) "helpfully" re-completes back to the full name plus
            # trailing separator -- which looks like backspace is broken,
            # since the user can never delete past a directory boundary.
            return
        suggestion = _suggest_completion(text)
        if suggestion and len(suggestion) > len(text):
            self.setText(suggestion)
            self.setCursorPosition(len(text))
            self.setSelection(len(text), len(suggestion) - len(text))

    def event(self, event) -> bool:
        # Tab has to be intercepted here, not in keyPressEvent(): Qt's
        # built-in Tab-to-next-widget focus traversal is decided at this
        # dispatch level, and isn't reliably suppressed just by consuming
        # the key in keyPressEvent() -- that looked like it worked in
        # testing (the text updates correctly) but focus silently moved on
        # to the next widget anyway. Returning True here, without calling
        # the base implementation at all for this event, is what actually
        # stops it.
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Tab:
            self._accept_tab_completion()
            return True
        return super().event(event)

    def _accept_tab_completion(self) -> None:
        if self.hasSelectedText():
            self.setCursorPosition(len(self.text()))  # accept the shown suggestion
        else:
            suggestion = _suggest_completion(self.text())
            if suggestion:
                self.setText(suggestion)
                self.end(False)
        # chain: just completed a folder -> immediately suggest what's next
        text = self.text()
        if text.endswith(("/", "\\")):
            nxt = _suggest_completion(text)
            if nxt and len(nxt) > len(text):
                self.setText(nxt)
                self.setCursorPosition(len(text))
                self.setSelection(len(text), len(nxt) - len(text))

    def keyPressEvent(self, event) -> None:
        self._deleting = event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
        super().keyPressEvent(event)


class FileOpenDialog(QDialog):
    def __init__(self, theme: ThemeManager, start_dir: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open HDF5 File")
        self.resize(600, 460)
        self._palette: Palette = theme.palette
        self._selected: Optional[Path] = None
        self._selected_row: Optional[_ClickableRow] = None

        try:
            self.current_dir = Path(start_dir).expanduser().resolve() if start_dir else Path.home()
            if not self.current_dir.is_dir():
                self.current_dir = Path.home()
        except OSError:
            self.current_dir = Path.home()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 16)
        outer.setSpacing(10)

        nav = QHBoxLayout()
        up_btn = QPushButton("↑")
        up_btn.setFixedWidth(32)
        up_btn.clicked.connect(self._go_up)
        nav.addWidget(up_btn)

        self.path_edit = _PathEdit()
        self.path_edit.returnPressed.connect(self._on_path_entered)
        nav.addWidget(self.path_edit, 1)

        home_btn = QPushButton("Home")
        home_btn.setFixedWidth(56)
        home_btn.clicked.connect(self._go_home)
        nav.addWidget(home_btn)
        outer.addLayout(nav)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_body = QWidget()
        self.list_layout = QVBoxLayout(self.list_body)
        self.list_layout.setContentsMargins(2, 2, 2, 2)
        self.list_layout.setSpacing(3)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_body)
        outer.addWidget(self.scroll, 1)

        self.error_label = QLabel("")
        outer.addWidget(self.error_label)

        footer = QHBoxLayout()
        self.selection_label = QLabel("No file selected")
        footer.addWidget(self.selection_label, 1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        self.open_button = QPushButton("Open")
        self.open_button.setEnabled(False)
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self.accept)
        footer.addWidget(self.open_button)
        outer.addLayout(footer)

        self._apply_palette(theme.palette)
        theme.register(self._apply_palette)
        self._refresh_listing()

    def get_path(self) -> Optional[str]:
        if self.exec() == QDialog.DialogCode.Accepted and self._selected is not None:
            return str(self._selected)
        return None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Without this, nothing in the dialog has keyboard focus when it
        # opens (QPushButton.setDefault() controls which button activates
        # on Enter, not keyboard focus) -- typing would go nowhere, which
        # looks exactly like "there's no autocomplete" from the outside.
        self.path_edit.setFocus()
        self.path_edit.end(False)

    # -- navigation ------------------------------------------------------

    def _go_up(self) -> None:
        if self.current_dir.parent != self.current_dir:
            self.current_dir = self.current_dir.parent
            self._refresh_listing()

    def _go_home(self) -> None:
        self.current_dir = Path.home()
        self._refresh_listing()

    def _on_path_entered(self) -> None:
        typed = Path(self.path_edit.text().strip()).expanduser()
        if typed.is_dir():
            self.current_dir = typed.resolve()
            self._refresh_listing()
        elif typed.is_file() and typed.suffix.lower() in H5_SUFFIXES:
            self.current_dir = typed.parent
            self._refresh_listing()
            self._select(typed)
        else:
            self.error_label.setText("Not a valid folder or .h5 file")

    def _refresh_listing(self) -> None:
        self.error_label.setText("")
        self._selected = None
        self._selected_row = None
        self.open_button.setEnabled(False)
        self.selection_label.setText("No file selected")
        self.path_edit.setText(str(self.current_dir))

        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            entries = list(self.current_dir.iterdir())
        except OSError as exc:
            self.error_label.setText(f"Can't read this folder: {exc.strerror or exc}")
            entries = []

        dirs = sorted(
            (e for e in entries if e.is_dir() and not e.name.startswith(".")), key=lambda p: p.name.lower()
        )
        files = sorted(
            (e for e in entries if e.is_file() and e.suffix.lower() in H5_SUFFIXES), key=lambda p: p.name.lower()
        )

        if not dirs and not files:
            empty = QLabel("No subfolders or .h5 files here.")
            empty.setStyleSheet(f"color: {self._palette.subtext};")
            self.list_layout.insertWidget(0, empty)
            return

        i = 0
        for d in dirs:
            i = self._add_row(d, is_dir=True, index=i)
        for f in files:
            i = self._add_row(f, is_dir=False, index=i)

    def _add_row(self, path: Path, is_dir: bool, index: int) -> int:
        row = _ClickableRow()
        row.setObjectName("fileRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 7, 12, 7)

        icon_label = QLabel()
        color = self._palette.subtext if is_dir else self._palette.accent
        icon_label.setPixmap(icons.pixmap(GROUP if is_dir else DATASET, color, ICON_SIZE))
        row_layout.addWidget(icon_label)

        name_label = QLabel(path.name)
        row_layout.addWidget(name_label, 1)

        if is_dir:
            row.clicked.connect(lambda p=path: self._enter_dir(p))
        else:
            row.clicked.connect(lambda p=path, r=row: self._select(p, r))
            row.doubleClicked.connect(self.accept)

        self.list_layout.insertWidget(index, row)
        return index + 1

    def _enter_dir(self, path: Path) -> None:
        self.current_dir = path
        self._refresh_listing()

    def _select(self, path: Path, row: Optional[_ClickableRow] = None) -> None:
        if self._selected_row is not None:
            self._selected_row.setProperty("selected", False)
            self._selected_row.style().unpolish(self._selected_row)
            self._selected_row.style().polish(self._selected_row)
        self._selected = path
        self._selected_row = row
        if row is not None:
            row.setProperty("selected", True)
            row.style().unpolish(row)
            row.style().polish(row)
        self.selection_label.setText(path.name)
        self.open_button.setEnabled(True)

    # -- theming ---------------------------------------------------------

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.error_label.setStyleSheet("color: #FF6B6B; font-size: 9pt;")
        self.selection_label.setStyleSheet(f"color: {palette.subtext};")
        self.setStyleSheet(
            f"""
            QDialog {{ background-color: {palette.window_bg}; color: {palette.text}; }}
            QLineEdit {{
                background-color: {palette.base_bg};
                border: 1px solid {palette.grid_line};
                border-radius: 6px;
                padding: 6px 8px;
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
            QFrame#fileRow {{
                background-color: {palette.header_bg};
                border-radius: 8px;
            }}
            QFrame#fileRow:hover {{
                background-color: {palette.row_hover};
            }}
            QFrame#fileRow[selected="true"] {{
                background-color: {palette.selection};
            }}
            """
        )
