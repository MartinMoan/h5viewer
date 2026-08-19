"""Shared visual constants: fonts, sizing and the column color palette.

Colors are defined as (light, dark) pairs so the rest of the app can pick
the right one for the current appearance mode without any widget needing
to know the palette itself.
"""
from __future__ import annotations

APP_NAME = "H5 Viewer"

ROW_HEIGHT = 27
HEADER_HEIGHT = 32
INDEX_COL_WIDTH = 60
MIN_COL_WIDTH = 90
MAX_COL_WIDTH = 260

# Fonts to try, in order, before giving up and letting Qt pick its
# platform default. Segoe UI / SF cover stock Windows/macOS; the rest are
# common Linux desktop fallbacks.
FONT_CANDIDATES = [
    "Segoe UI",
    "SF Pro Text",
    "Helvetica Neue",
    "Inter",
    "Roboto",
    "Ubuntu",
    "Noto Sans",
    "Cantarell",
    "DejaVu Sans",
    "Arial",
]

# Number of rows fetched from disk per background read. Small enough to
# stay responsive while scrolling, large enough to amortize h5py call
# overhead.
ROW_BLOCK_SIZE = 200

# How many blocks (i.e. ROW_BLOCK_SIZE * this) to keep resident in memory
# per open dataset view before evicting least-recently-used blocks.
MAX_CACHED_BLOCKS = 400

# Safety cap on the number of flattened columns we will ever try to render
# for a single dataset (very wide / high-rank datasets get truncated with
# a warning rather than freezing the UI).
MAX_COLUMNS = 256

# Hard cap on rows read into memory for a "graph selected columns" plot.
# This is a one-shot bulk read via h5_model.read_rows(), independent of
# the table's own ROW_BLOCK_SIZE/MAX_CACHED_BLOCKS paging cache -- kept
# well under that cache's own 80,000-row resident cap, and low enough to
# keep SVG-mode Plotly traces (Line/Bar/Histogram) panning/zooming
# smoothly without needing WebGL for every trace type.
MAX_PLOT_ROWS = 20_000

# Background colors used to tint alternating columns so it's easy to track
# a column while scrolling vertically through a large table.
COLUMN_PALETTE_LIGHT = [
    "#FFFFFF",
    "#F2F6FC",
    "#EEF7F1",
    "#FCF3EC",
    "#F6F0FA",
    "#EFFAFA",
]
COLUMN_PALETTE_DARK = [
    "#242424",
    "#20262E",
    "#1E2A22",
    "#2A2420",
    "#26202C",
    "#1D2828",
]

# Text colors for header / body per appearance mode.
TEXT_LIGHT = "#1A1A1A"
TEXT_DARK = "#E6E6E6"
SUBTEXT_LIGHT = "#6B6B6B"
SUBTEXT_DARK = "#9A9A9A"

HEADER_BG_LIGHT = "#E5E9F0"
HEADER_BG_DARK = "#2B2B2B"

GRID_LINE_LIGHT = "#DDE1E8"
GRID_LINE_DARK = "#3A3A3A"

SELECTION_LIGHT = "#E6E4FB"
SELECTION_DARK = "#37316B"

# Accent used for the active/interactive accents that aren't covered by
# the CTk color theme itself (ttk selection, splitter drag feedback,
# dataset-type icon tint).
ACCENT_LIGHT = "#4F46E5"
ACCENT_DARK = "#8B85F5"

ROW_HOVER_LIGHT = "#F1F1F6"
ROW_HOVER_DARK = "#272733"

SPLITTER_LIGHT = "#E3E5EA"
SPLITTER_DARK = "#303030"

# Truncation/warning text color -- used both for the dataset table's
# "showing first N of M flattened columns" notice and the graph window's
# "showing first N of M rows" notice.
WARN_COLOR_LIGHT = "#8A5A00"
WARN_COLOR_DARK = "#E0A93B"

# Vivid line/marker colors for the graph window, index-aligned with
# COLUMN_PALETTE_* so a plotted series' color still matches the hue family
# of its column's tint in the table -- but NOT the same colors: those are
# deliberately near-invisible tints meant to sit almost flush with the
# table's own background (e.g. dark index 1 "#20262E" vs body_bg
# "#242424"), which made an actual plotted line nearly invisible against
# the chart background when reused directly as a line color.
CHART_SERIES_LIGHT = [
    ACCENT_LIGHT,  # index 0 (the default/unstyled column) -- no real hue of its own
    "#2563EB",  # blue
    "#059669",  # green
    "#D97706",  # orange
    "#7C3AED",  # purple
    "#0D9488",  # teal
]
CHART_SERIES_DARK = [
    ACCENT_DARK,
    "#5B9BFF",  # blue
    "#4ADE80",  # green
    "#FB923C",  # orange
    "#C084FC",  # purple
    "#2DD4BF",  # teal
]
