"""Custom, theme-matched title bar used in place of the native OS window
frame.

Consistent styling across Windows/Linux/WSL was the whole point of moving
to Qt, and the native window frame is drawn by the OS/window manager --
Qt has no more hook to restyle *that* than Tk did. Move/resize/maximize
all use Qt's native, OS-assisted window operations
(``startSystemMove``/``startSystemResize``/``showMaximized``), not
hand-rolled geometry math, so window snapping, multi-monitor behavior,
etc. all keep working the way the OS expects. Unlike the earlier Tk
prototype, minimize is included here: Qt's ``FramelessWindowHint`` is a
first-class, properly WM-aware window flag rather than a raw X11
override-redirect hack, so ``showMinimized()`` is expected to just work.

The File/Settings/Help menu bar lives here too, VS Code-style, rather
than as a separate toolbar row below the title bar.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QActionGroup, QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenuBar, QMessageBox, QPushButton, QWidget

from .. import icons
from ..theme import Palette, ThemeManager

BAR_HEIGHT = 34
ICON_SIZE = 11


class TitleBar(QWidget):
    def __init__(
        self,
        theme: ThemeManager,
        title: str,
        on_open_file: Callable[[], None],
        on_set_appearance: Callable[[str], None],
        on_minimize: Callable[[], None],
        on_toggle_maximize: Callable[[], None],
        on_close: Callable[[], None],
        parent=None,
    ):
        super().__init__(parent)
        self.setFixedHeight(BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._app_title = title
        self._palette: Palette = theme.palette
        self._maximized = False
        self._on_toggle_maximize = on_toggle_maximize

        layout = QHBoxLayout(self)
        # 22px left margin, not some rounder number: measured to match the
        # hierarchy tree's own effective left inset (its layout margin
        # plus QTreeView's built-in icon/branch spacing) so the title text
        # here lines up with the sidebar's content below it, edge to edge.
        layout.setContentsMargins(22, 0, 0, 0)
        layout.setSpacing(10)

        # Far left, before the menu -- currently just app-name text, but
        # likely to become an icon later (hence living in its own label
        # rather than, say, being folded into the menu bar's corner
        # widget).
        self.title_label = QLabel(title)
        layout.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.menu_bar = self._build_menu_bar(on_open_file, on_set_appearance, on_close)
        # AlignVCenter matters here: without it, the layout stretches the
        # menu bar to the row's full height, and QMenuBar renders its
        # items top-aligned within whatever height it's given rather than
        # centering them the way QLabel centers text by default -- items
        # ended up flush against the top with a gap below, not centered.
        # Explicitly not stretching it vertically (so it stays at its own
        # sizeHint height) and centering that box in the row fixes it.
        layout.addWidget(self.menu_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)

        self.min_button = self._make_button(icons.MINIMIZE, on_minimize)
        self.max_button = self._make_button(icons.MAXIMIZE, on_toggle_maximize)
        self.close_button = self._make_button(icons.CLOSE, on_close, close=True)
        layout.addWidget(self.min_button)
        layout.addWidget(self.max_button)
        layout.addWidget(self.close_button)

        theme.register(self._apply_palette)

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = maximized
        kind = icons.RESTORE if maximized else icons.MAXIMIZE
        self.max_button.setIcon(icons.icon(kind, self._palette.text, ICON_SIZE))

    # -- menu bar ----------------------------------------------------------

    def _build_menu_bar(self, on_open_file, on_set_appearance, on_close) -> QMenuBar:
        bar = QMenuBar(self)
        bar.setNativeMenuBar(False)  # always render in-window, never as a platform global menu bar

        file_menu = bar.addMenu("File")
        open_action = file_menu.addAction("Open File…")
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(on_open_file)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(on_close)

        settings_menu = bar.addMenu("Settings")
        appearance_menu = settings_menu.addMenu("Appearance")
        appearance_group = QActionGroup(self)
        appearance_group.setExclusive(True)
        self._appearance_actions = {}
        for mode in ("System", "Light", "Dark"):
            action = appearance_menu.addAction(mode)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked, m=mode.lower(): on_set_appearance(m))
            appearance_group.addAction(action)
            self._appearance_actions[mode.lower()] = action
        self._appearance_actions["dark"].setChecked(True)  # matches the app's default

        help_menu = bar.addMenu("Help")
        about_action = help_menu.addAction(f"About {self._app_title}")
        about_action.triggered.connect(self._show_about)

        return bar

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {self._app_title}",
            f"<b>{self._app_title}</b><br>A modern, cross-platform viewer for HDF5 (.h5) files.",
        )

    def _make_button(self, kind: str, callback, close: bool = False) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(icons.icon(kind, self._palette.text, ICON_SIZE))
        btn.setFixedSize(46, BAR_HEIGHT)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.ArrowCursor)
        btn.setObjectName("titleCloseButton" if close else "titleButton")
        btn.clicked.connect(callback)
        return btn

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_toggle_maximize()
        super().mouseDoubleClickEvent(event)

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.title_label.setStyleSheet(f"color: {palette.subtext}; font-weight: 600; font-size: 10pt;")
        self.setStyleSheet(
            f"""
            TitleBar {{ background-color: {palette.header_bg}; }}
            QMenuBar {{
                background: transparent;
                color: {palette.text};
                spacing: 2px;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 6px 10px;
                border-radius: 6px;
            }}
            QMenuBar::item:selected {{ background-color: {palette.row_hover}; }}
            QMenuBar::item:pressed {{ background-color: {palette.accent}; color: white; }}
            QMenu {{
                background-color: {palette.base_bg};
                color: {palette.text};
                border: 1px solid {palette.grid_line};
                padding: 4px;
            }}
            QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: {palette.row_hover}; }}
            QMenu::separator {{ height: 1px; background-color: {palette.grid_line}; margin: 4px 6px; }}
            QPushButton#titleButton {{ border: none; background: transparent; }}
            QPushButton#titleButton:hover {{ background-color: {palette.row_hover}; }}
            QPushButton#titleCloseButton {{ border: none; background: transparent; }}
            QPushButton#titleCloseButton:hover {{ background-color: #E81123; }}
            """
        )
        self.set_maximized(self._maximized)
