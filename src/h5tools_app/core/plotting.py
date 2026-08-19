"""Turns a set of selected dataset columns into a Plotly.js figure spec.

Pure Python/numpy -- no Qt, no WebEngine -- so it's unit-testable on its
own. The actual rendering happens in ``widgets/graph_window.py``, which
hands ``build_plotly_spec``'s output to a ``QWebEngineView`` running the
vendored ``plotly.min.js`` (see ``assets/``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import h5py
import numpy as np

from .. import constants as c
from ..theme import Palette
from .h5_model import ColumnLayout, read_rows


class ChartType(str, Enum):
    LINE = "line"
    SCATTER = "scatter"
    BAR = "bar"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class GraphConfig:
    x_column: int
    series: dict  # {column_index: ChartType}


def fetch_columns(
    dataset: h5py.Dataset,
    layout: ColumnLayout,
    col_indices: list,
    max_rows: int = c.MAX_PLOT_ROWS,
) -> tuple:
    """One-shot bulk read of ``col_indices`` as 1-D float arrays, capped at
    ``max_rows``. Bypasses DatasetSource's block-paging cache entirely --
    this is a single read, not something scrolled through incrementally.

    Returns ``(arrays_by_col_index, truncated)`` where ``truncated`` is
    True if ``layout.row_count`` exceeds ``max_rows``.
    """
    end = min(layout.row_count, max_rows)
    truncated = layout.row_count > max_rows
    block = read_rows(dataset, 0, end, layout)
    arrays = {col: np.asarray(block[:, col], dtype=float) for col in col_indices}
    return arrays, truncated


def _trace_type_mode(chart_type: ChartType) -> dict:
    if chart_type == ChartType.LINE:
        return {"type": "scatter", "mode": "lines"}
    if chart_type == ChartType.SCATTER:
        # Plain SVG "scatter" (mode: markers), not "scattergl" -- scattergl
        # renders via WebGL, and this app has repeatedly hit flaky/failing
        # GPU contexts in QWebEngineView during development ("GPU state
        # invalid", failed command buffers). A WebGL trace silently
        # rendering wrong (e.g. losing its color) when the GPU context is
        # unhappy, while every other SVG-rendered trace type keeps working
        # fine, exactly matches "colors work for line plots but not
        # scatter." At MAX_PLOT_ROWS's cap, plain SVG markers render just
        # as well as WebGL ones -- not worth the correctness risk.
        return {"type": "scatter", "mode": "markers"}
    if chart_type == ChartType.BAR:
        return {"type": "bar"}
    return {"type": "histogram"}


def build_plotly_spec(labels: list, config: GraphConfig, arrays: dict, palette: Palette) -> dict:
    """Builds the full ``{"data": [...], "layout": {...}}`` Plotly spec.

    Line/Scatter/Bar series share one set of axes, rows sorted ascending
    by the X column (Plotly draws 'lines' traces in array order, not
    sorted by x, so an unsorted X column would render as a zig-zag).
    Histogram series -- which bin a column's own values and have no
    meaningful pairing with an X column -- get a separate subplot stacked
    below, overlaid together. If every series is a Histogram, the primary
    axes are skipped entirely.
    """
    x_col = config.x_column
    primary_cols = [col for col, kind in config.series.items() if kind != ChartType.HISTOGRAM]
    hist_cols = [col for col, kind in config.series.items() if kind == ChartType.HISTOGRAM]

    data = []

    if primary_cols:
        x_values = arrays[x_col]
        order = np.argsort(x_values)
        sorted_x = x_values[order]
        for col in primary_cols:
            trace = {
                "name": labels[col],
                "x": sorted_x.tolist(),
                "y": arrays[col][order].tolist(),
                "marker": {"color": palette.chart_color(col)},
                "line": {"color": palette.chart_color(col)},
            }
            trace.update(_trace_type_mode(config.series[col]))
            if hist_cols:
                trace["xaxis"] = "x"
                trace["yaxis"] = "y"
            data.append(trace)

    for col in hist_cols:
        trace = {
            "name": labels[col],
            "x": arrays[col].tolist(),
            "type": "histogram",
            "opacity": 0.65,
            "marker": {"color": palette.chart_color(col)},
        }
        if primary_cols:
            trace["xaxis"] = "x2"
            trace["yaxis"] = "y2"
        data.append(trace)

    # A single series gets its column name as the axis title directly;
    # multiple series sharing one axis have no single unambiguous title,
    # so that axis is left untitled and disambiguated via the legend
    # (always shown, see _base_layout) and the hover tooltip instead.
    x_title = labels[x_col]
    primary_y_title = labels[primary_cols[0]] if len(primary_cols) == 1 else None
    hist_x_title = labels[hist_cols[0]] if len(hist_cols) == 1 else None

    layout = _base_layout(palette)
    if primary_cols and hist_cols:
        # Two stacked subplots: primary group on top, histograms below.
        layout["grid"] = {"rows": 2, "columns": 1, "pattern": "independent"}
        layout["xaxis"] = {**_x_axis_style(palette, x_title), "domain": [0, 1], "anchor": "y"}
        layout["yaxis"] = {**_axis_style(palette, primary_y_title), "domain": [0.55, 1], "anchor": "x"}
        layout["xaxis2"] = {**_x_axis_style(palette, hist_x_title), "domain": [0, 1], "anchor": "y2"}
        layout["yaxis2"] = {**_axis_style(palette, "Count"), "domain": [0, 0.4], "anchor": "x2"}
        layout["hovermode"] = "x unified"
        layout["barmode"] = "overlay"
    elif primary_cols:
        layout["xaxis"] = _x_axis_style(palette, x_title)
        layout["yaxis"] = _axis_style(palette, primary_y_title)
        layout["hovermode"] = "x unified"
    else:
        layout["xaxis"] = _x_axis_style(palette, hist_x_title)
        layout["yaxis"] = _axis_style(palette, "Count")
        layout["barmode"] = "overlay"

    return {"data": data, "layout": layout}


def _axis_style(palette: Palette, title: str | None = None) -> dict:
    style = {
        "gridcolor": palette.grid_line,
        "zerolinecolor": palette.grid_line,
        "color": palette.text,
        "linecolor": palette.grid_line,
    }
    if title:
        style["title"] = {"text": title, "font": {"color": palette.subtext}}
    return style


def _x_axis_style(palette: Palette, title: str | None = None) -> dict:
    return {
        **_axis_style(palette, title),
        "showspikes": True,
        "spikemode": "across",
        # 'cursor' (continuous, follows the pointer's exact pixel position)
        # instead of the default 'hovered data' (snaps to the nearest
        # actual data point) -- with the default, the vertical hover line
        # only jumps between discrete data-point x-positions, which reads
        # as the line "lagging behind" a smoothly-moving cursor rather
        # than tracking it.
        "spikesnap": "cursor",
        "spikedash": "dot",
        "spikethickness": 1,
        "spikecolor": palette.subtext,
    }


def _base_layout(palette: Palette) -> dict:
    return {
        "paper_bgcolor": palette.base_bg,
        "plot_bgcolor": palette.base_bg,
        "font": {"color": palette.text},
        # Horizontal, centered, below the plot -- not Plotly's default
        # (vertical, right side), which would collide with the custom
        # toolbar floating over the top-right corner (see graph_window.py).
        "legend": {
            "font": {"color": palette.text},
            "orientation": "h",
            "x": 0.5,
            "xanchor": "center",
            "y": -0.16,
            "yanchor": "top",
        },
        "showlegend": True,
        "hoverlabel": {"bgcolor": palette.button_bg, "font": {"color": palette.text}},
        "margin": {"l": 60, "r": 30, "t": 30, "b": 70},
        # Plotly's own built-in modebar is turned off entirely (see
        # _PLOTLY_CONFIG in graph_window.py, displayModeBar: false) in
        # favor of a custom-styled floating toolbar built directly into
        # the page there -- its stock icon set read as dated next to the
        # rest of this app's hand-drawn icon language.
    }
