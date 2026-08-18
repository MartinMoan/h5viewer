# H5 Viewer

A modern, cross-platform desktop viewer for HDF5 (`.h5`) files, built with
[PySide6 (Qt for Python)](https://doc.qt.io/qtforpython-6/) and
[h5py](https://www.h5py.org/).

- Left pane: a VS Code-style explorer over the file's group hierarchy —
  expand/collapse groups, lazily loading children as you go.
- Right pane: select a group to see its attributes and contents, or select
  a dataset to open it as a table.
- The table view scrolls continuously over the whole dataset (no
  pagination) while only ever holding a small window of rows in memory —
  see "How large datasets are handled" below.
- Columns are tinted with alternating colors so it's easy to track a
  column visually while scrolling through a lot of rows.
- Starts in dark mode by default; System/Light/Dark are available under
  Settings > Appearance.
- Frameless window with a custom title bar, consistent across platforms —
  see "Why Qt, and why frameless" below. The File/Settings/Help menu bar
  lives in the title bar itself (VS Code-style) rather than a separate
  toolbar row; the open file's name shows in the title bar text.

## Why Qt, and why frameless

This app used to be Tkinter/CustomTkinter. It moved to Qt because the
goal is for it to look and behave *identically* on native Windows, native
Linux, and WSL — and Tk fundamentally can't do that: `ttk` widgets
delegate to whatever native theming engine the OS provides, so the same
app looks different (and, on a minimal WSLg setup, noticeably dated) on
each platform. Qt is forced into its "Fusion" style here (see
`theme.py`), which Qt draws itself, pixel for pixel the same regardless
of OS — the same idea VS Code uses (a bundled rendering engine instead of
native widgets), just via Qt's own widget set instead of a browser
engine, which let almost the entire Python/h5py backend from the Tk
version carry over unchanged (`core/h5_model.py`, `core/dataset_source.py`
— the lazy navigation and the threaded chunked dataset loader).

The window itself is frameless (`Qt.FramelessWindowHint`) with a
hand-drawn title bar (`widgets/title_bar.py`), because the native window
frame is drawn by the OS/window manager and no toolkit can restyle that.
Move/resize use Qt's native, OS-assisted window operations
(`startSystemMove`/`startSystemResize`), not hand-rolled geometry math —
this matters because an earlier Tk prototype hand-rolled frameless
support via `overrideredirect`, which turned out to reliably **crash the
X connection** under WSLg the moment `iconify()` was called on a window
that had ever had its frame toggled off. Qt's frameless support is a
proper, first-class, widely-shipped window flag rather than a raw X11
hack, and minimize (`showMinimized()`) was verified safe in the exact
same environment that broke under Tk. Maximize is the one place Qt's own
`showMaximized()` wasn't trustworthy either: on a frameless X11 window it
doesn't reliably know there are no decorations to account for and can
leave the window offset from the screen edge, so maximize/restore sets
geometry to the screen's available rect directly instead (`app.py`,
`_toggle_maximize`).

"Open File…" is a custom `QDialog` (`widgets/file_open_dialog.py`)
rather than Qt's stock `QFileDialog` — the stock dialog only inherits our
`QApplication`-wide palette (base colors), not the app's QSS styling
(rounded rows, accent icons, hover states), since stylesheets don't
cascade across separate top-level windows; left alone it looks like a
plain, dated Fusion dialog. It's still a real `QDialog` though (properly
parented to the main window), so it doesn't have the stacking/focus bugs
an unparented Tk `Toplevel` had. Path entry uses Qt's own
`QCompleter`+`QFileSystemModel` for native type-ahead completion.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

**Linux troubleshooting:** if you see an error like *"could not load the
Qt platform plugin xcb"* mentioning `libxcb-cursor0`, install it (and Qt
will generally tell you exactly which package it's missing):

```bash
sudo apt install libxcb-cursor0     # Debian/Ubuntu/WSL
sudo dnf install xcb-util-cursor    # Fedora
```

This is normally already present on a full desktop Linux install (pulled
in by the desktop environment); it only tends to be missing on minimal/
headless installs or bare WSL distros without a desktop environment
installed.

## Running

```bash
python run.py                   # opens with no file loaded; use "Open File…"
python run.py path/to/data.h5   # opens a file directly
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The test suite exercises the h5py-facing logic in `h5tools_app.core`
(tree navigation, column layout, threaded/cached row loading) against
generated temporary `.h5` files — it's UI-framework-agnostic and doesn't
drive the GUI itself, which is why it survived the Tk → Qt rewrite
unchanged.

## Project layout

```
run.py                          entry point: python run.py [file.h5]
src/h5tools_app/
  app.py                        main window + frameless window chrome
  theme.py                      Fusion style, QPalette, light/dark detection
  constants.py                  sizing + column color palette
  icons.py                      Pillow-drawn icons -> QPixmap/QIcon
  core/
    h5_model.py                 lazy h5py navigation, column-layout logic
    dataset_source.py           threaded, cached, chunked row reader
  widgets/
    title_bar.py                 custom frameless-window title bar
    file_open_dialog.py           custom-styled "Open File" QDialog
    hierarchy_tree.py           left-pane explorer (QTreeView)
    dataset_table.py            virtualized table (QTableView + model)
    group_panel.py              overview shown for groups / file root
    status_bar.py                bottom status strip
tests/                          pytest suite for core/ (no GUI)
```

## How large datasets are handled

Opening a dataset never reads it into memory. Instead:

1. The dataset's shape/dtype is turned into a flat column layout (an N-D
   or compound dataset gets its trailing dimensions and/or field names
   flattened into columns; extremely wide datasets are capped at 256
   columns with a warning banner rather than freezing the UI).
2. `DatasetTableModel` (a `QAbstractTableModel`) reports the dataset's
   real row count, but Qt's `QTableView` only ever calls `data()` for
   cells that are actually on screen — virtualization is built into Qt's
   model/view framework, not something hand-rolled here.
3. When `data()` is asked for a row whose block hasn't loaded yet, it
   returns a `···` placeholder immediately and kicks off a background
   load via `DatasetSource`, which reads fixed-size blocks (200 rows by
   default) from disk on a single worker thread — h5py's slicing already
   only touches the requested region of the file — and caches recently
   used blocks (LRU-ish eviction) so scrolling back is instant.
4. A small `QTimer` polls the source and emits `dataChanged` for whatever
   finished loading since the last tick, so the view redraws itself
   automatically as data arrives.

This is what lets the view scroll smoothly and continuously from row 0 to
row N even when N is very large, without ever pausing to "page" or
loading more than a sliver of the dataset into memory at once. A "Row #"
jump box plus Top/End buttons are provided for quickly navigating within
huge datasets without physically scrolling the whole way. (One real limit
worth knowing: Qt's row counts are 32-bit, so a single dataset topping
roughly 2.1 billion rows would need a different approach — not something
either the old Tk version or this one handles.)

## Building a standalone installer

Not done yet — worth planning for since packaging is a real step, not an
afterthought:

- **Windows / Linux / macOS**, each needs its *own* build — PyInstaller
  and Qt's own `pyside6-deploy` (which wraps Nuitka) both produce a
  platform-native executable, but only for the OS you run them on; there's
  no reliable cross-compilation path. From this sandbox I can only
  build/verify the Linux path.
- A typical flow per OS:
  - `pip install pyinstaller` then
    `pyinstaller --windowed --name "H5 Viewer" run.py`, or
    `pyside6-deploy` (official Qt tool, needs a `pysidedeploy.spec`).
  - **Windows**: wrap the produced `.exe`/folder with
    [Inno Setup](https://jrsoftware.org/isinfo.php) or NSIS to get a real
    installer with Start Menu entries and an uninstaller.
  - **Linux**: package as an `.AppImage` via `linuxdeploy` +
    `linuxdeploy-plugin-qt`, or a `.deb`/`.rpm`.
  - **macOS**: bundle as a `.app`, package as a `.dmg`; distributing
    outside your own machine needs Apple code signing/notarization
    (requires an Apple Developer account).
- The practical way to get all three without owning three machines is a
  CI matrix (GitHub Actions `windows-latest` / `ubuntu-latest` /
  `macos-latest` runners, each building and uploading its own installer).
  This repo isn't a git repository yet, so that's a separate step — happy
  to set up the workflow file once there's a remote to push it to.
