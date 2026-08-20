"""Turns a set of selected dataset columns into a Plotly.js figure spec.

Pure Python/numpy -- no Qt, no WebEngine -- so it's unit-testable on its
own. The actual rendering happens in ``widgets/graph_window.py``, which
hands ``build_plotly_spec``'s (or ``build_map_plotly_spec``'s) output to
a ``QWebEngineView`` running the vendored ``plotly.min.js`` (see
``assets/``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import h5py
import numpy as np

from .. import constants as c
from ..theme import Palette
from .basemap import crop_raster_to_view, crop_vector_to_view, load_basemap
from .h5_model import ColumnLayout, read_rows


class ChartType(str, Enum):
    LINE = "line"
    AREA = "area"
    SCATTER = "scatter"
    BAR = "bar"
    HISTOGRAM = "histogram"
    BOX = "box"
    VIOLIN = "violin"


# Chart types that summarize a column's own value distribution rather
# than pairing it with the shared X column -- each gets its own subplot,
# same idea as Histogram always has, just generalized to three types
# instead of one (see build_plotly_spec's axis-slot assignment below).
_DISTRIBUTION_TYPES = (ChartType.HISTOGRAM, ChartType.BOX, ChartType.VIOLIN)


@dataclass(frozen=True)
class SeriesSpec:
    chart_type: ChartType
    # Which Y axis this series plots against -- "left" (the default,
    # shared primary axis) or "right" (a second, independently-scaled
    # axis overlaid on the same plot area). Ignored for the distribution
    # types (see _DISTRIBUTION_TYPES), which always get their own subplot
    # regardless of this field.
    axis: str = "left"
    # Scatter-only bubble-chart mapping: an extra column's values drive
    # marker color (continuous colorscale) and/or marker size (area-
    # scaled). None means "not mapped" -- a plain single-color marker.
    color_by: Optional[int] = None
    size_by: Optional[int] = None


@dataclass(frozen=True)
class GraphConfig:
    x_column: int
    series: dict  # {column_index: SeriesSpec}
    # Only matters when 2+ Bar-type series are present -- see the
    # barmode-precedence comment in build_plotly_spec for why this can't
    # always simply be honored (Plotly's barmode is figure-global, not
    # per-subplot).
    bar_mode: str = "group"  # "group" | "stack"
    log_x: bool = False
    log_y_left: bool = False
    log_y_right: bool = False

    def columns_used(self) -> list:
        cols = [self.x_column]
        for col, spec in self.series.items():
            cols.append(col)
            if spec.color_by is not None:
                cols.append(spec.color_by)
            if spec.size_by is not None:
                cols.append(spec.size_by)
        return cols


@dataclass(frozen=True)
class MapConfig:
    lat_column: int
    lon_column: int
    color_by: Optional[int] = None
    size_by: Optional[int] = None
    connect_points: bool = False
    # Path to a user-supplied basemap file (GeoJSON, or a georeferenced
    # image) -- None means "no custom basemap", i.e. the built-in offline
    # world-outline geo map (see build_map_plotly_spec / core/basemap.py).
    basemap_path: Optional[str] = None
    # Some instruments/logs record lat/lon in radians rather than
    # degrees -- "degrees" (the default) or "radians". Converted to
    # degrees once, right at the top of build_map_plotly_spec, so
    # everything downstream of that (axis titles, aspect-ratio
    # correction, basemap cropping against a file's own degree-based
    # bounds) only ever has to deal with one unit.
    lat_lon_units: str = "degrees"

    def columns_used(self) -> list:
        cols = [self.lat_column, self.lon_column]
        if self.color_by is not None:
            cols.append(self.color_by)
        if self.size_by is not None:
            cols.append(self.size_by)
        return cols


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
    if chart_type in (ChartType.LINE, ChartType.AREA):
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


def _translucent(hex_color: str, alpha: float = 0.25) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _bubble_sizes(values: np.ndarray, min_px: float = 6.0, max_px: float = 24.0) -> np.ndarray:
    """Scales ``values`` into a marker-size range, by *area* rather than
    radius -- a plain linear radius scale visually exaggerates
    differences (a common bubble-chart mistake), so this normalizes into
    [0, 1] first and then takes the square root."""
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    span = hi - lo
    norm = np.zeros_like(values) if span == 0 else (values - lo) / span
    norm = np.clip(np.nan_to_num(norm, nan=0.0), 0.0, 1.0)
    return min_px + np.sqrt(norm) * (max_px - min_px)


def _colorbar_marker(values: np.ndarray, title: str, palette: Palette) -> dict:
    return {
        "color": values.tolist(),
        "colorscale": "Viridis",
        "showscale": True,
        "colorbar": {
            "title": {"text": title, "font": {"color": palette.subtext}},
            "tickfont": {"color": palette.subtext},
            "outlinewidth": 0,
        },
    }


def _marker_style(col: int, spec: SeriesSpec, arrays: dict, labels: list, palette: Palette) -> dict:
    if spec.chart_type != ChartType.SCATTER or (spec.color_by is None and spec.size_by is None):
        return {"color": palette.chart_color(col)}
    marker: dict = (
        _colorbar_marker(arrays[spec.color_by], labels[spec.color_by], palette)
        if spec.color_by is not None
        else {"color": palette.chart_color(col)}
    )
    if spec.size_by is not None:
        marker["size"] = _bubble_sizes(arrays[spec.size_by]).tolist()
    return marker


def _ref(prefix: str, slot: int) -> str:
    return prefix if slot == 1 else f"{prefix}{slot}"


def _layout_key(prefix: str, slot: int) -> str:
    return f"{prefix}axis" if slot == 1 else f"{prefix}axis{slot}"


def build_plotly_spec(labels: list, config: GraphConfig, arrays: dict, palette: Palette) -> dict:
    """Builds the full ``{"data": [...], "layout": {...}}`` Plotly spec.

    Line/Area/Scatter/Bar series ("primary") share one set of axes, rows
    sorted ascending by the X column (Plotly draws 'lines' traces in
    array order, not sorted by x, so an unsorted X column would render as
    a zig-zag) -- except that a series marked ``axis="right"`` (see
    SeriesSpec) plots against a second, independently-scaled Y axis
    overlaid on the same plot area instead, for pairing columns with very
    different value ranges (e.g. temperature and pressure) without one of
    them flattening into a barely-visible line.

    Histogram/Box/Violin series -- which summarize a column's own values
    and have no meaningful pairing with an X column -- each get their own
    separate subplot, stacked below whichever other groups are present.
    Axis "slot" numbers are assigned so the primary group (if present)
    always keeps the unnumbered "x"/"y" pair (with "y3" reserved for its
    own right-axis overlay, used or not), and Histogram/Box-and-Violin
    take slots 2/4 as needed to avoid colliding with that reservation --
    or the plain "x"/"y" pair themselves if there's no primary group at
    all.
    """
    x_col = config.x_column
    primary_cols = [col for col, spec in config.series.items() if spec.chart_type not in _DISTRIBUTION_TYPES]
    hist_cols = [col for col, spec in config.series.items() if spec.chart_type == ChartType.HISTOGRAM]
    bv_cols = [
        col
        for col, spec in config.series.items()
        if spec.chart_type in (ChartType.BOX, ChartType.VIOLIN)
    ]
    right_cols = [col for col in primary_cols if config.series[col].axis == "right"]
    left_cols = [col for col in primary_cols if col not in right_cols]
    bar_cols = [col for col in primary_cols if config.series[col].chart_type == ChartType.BAR]

    has_primary, has_hist, has_bv = bool(primary_cols), bool(hist_cols), bool(bv_cols)

    primary_slot = hist_slot = bv_slot = None
    if has_primary:
        primary_slot = 1
        pool = iter((2, 4))
        if has_hist:
            hist_slot = next(pool)
        if has_bv:
            bv_slot = next(pool)
    elif has_hist:
        hist_slot = 1
        if has_bv:
            bv_slot = 2
    elif has_bv:
        bv_slot = 1

    data = []

    if primary_cols:
        x_values = arrays[x_col]
        order = np.argsort(x_values)
        sorted_x = x_values[order]
        for col in primary_cols:
            spec = config.series[col]
            trace = {
                "name": labels[col],
                "x": sorted_x.tolist(),
                "y": arrays[col][order].tolist(),
                "marker": _marker_style(col, spec, arrays, labels, palette),
                "line": {"color": palette.chart_color(col)},
                "xaxis": _ref("x", primary_slot),
                "yaxis": "y3" if col in right_cols else _ref("y", primary_slot),
            }
            trace.update(_trace_type_mode(spec.chart_type))
            if spec.chart_type == ChartType.AREA:
                trace["fill"] = "tozeroy"
                trace["fillcolor"] = _translucent(palette.chart_color(col))
            data.append(trace)

    for col in hist_cols:
        trace = {
            "name": labels[col],
            "x": arrays[col].tolist(),
            "type": "histogram",
            "opacity": 0.65,
            "marker": {"color": palette.chart_color(col)},
            "xaxis": _ref("x", hist_slot),
            "yaxis": _ref("y", hist_slot),
        }
        data.append(trace)

    for col in bv_cols:
        spec = config.series[col]
        trace = {
            "name": labels[col],
            "y": arrays[col].tolist(),
            "type": "box" if spec.chart_type == ChartType.BOX else "violin",
            "marker": {"color": palette.chart_color(col)},
            "line": {"color": palette.chart_color(col)},
            "xaxis": _ref("x", bv_slot),
            "yaxis": _ref("y", bv_slot),
        }
        data.append(trace)

    # A single series on an axis gets its column name -- and, since the
    # color is then unambiguous too, its column's chart color -- as that
    # axis's title directly; multiple series sharing one axis have no
    # single unambiguous title/color, so that axis is left neutral and
    # disambiguated via the legend (always shown, see _base_layout) and
    # the hover tooltip instead.
    x_title = labels[x_col]
    left_y_title = labels[left_cols[0]] if len(left_cols) == 1 else None
    left_accent = palette.chart_color(left_cols[0]) if len(left_cols) == 1 else None
    right_y_title = labels[right_cols[0]] if len(right_cols) == 1 else None
    right_accent = palette.chart_color(right_cols[0]) if len(right_cols) == 1 else None
    hist_x_title = labels[hist_cols[0]] if len(hist_cols) == 1 else None
    bv_y_title = labels[bv_cols[0]] if len(bv_cols) == 1 else None

    n_rows = sum([has_primary, has_hist, has_bv])
    domains = _stacked_domains(n_rows) if n_rows > 1 else None

    layout = _base_layout(palette)
    row_i = 0

    if has_primary:
        dom = domains[row_i] if domains else None
        layout[_layout_key("x", primary_slot)] = _positioned(
            _x_axis_style(palette, x_title, config.log_x), dom, _ref("y", primary_slot)
        )
        layout[_layout_key("y", primary_slot)] = _positioned(
            _axis_style(palette, left_y_title, left_accent, config.log_y_left), dom, _ref("x", primary_slot)
        )
        if right_cols:
            layout["yaxis3"] = _secondary_y_axis_style(palette, right_y_title, right_accent, config.log_y_right)
            if dom:
                layout["yaxis3"]["domain"] = list(dom)
        row_i += 1

    if has_hist:
        dom = domains[row_i] if domains else None
        layout[_layout_key("x", hist_slot)] = _positioned(
            _x_axis_style(palette, hist_x_title), dom, _ref("y", hist_slot)
        )
        layout[_layout_key("y", hist_slot)] = _positioned(
            _axis_style(palette, "Count"), dom, _ref("x", hist_slot)
        )
        row_i += 1

    if has_bv:
        dom = domains[row_i] if domains else None
        layout[_layout_key("x", bv_slot)] = _positioned(_x_axis_style(palette), dom, _ref("y", bv_slot))
        layout[_layout_key("y", bv_slot)] = _positioned(
            _axis_style(palette, bv_y_title), dom, _ref("x", bv_slot)
        )
        row_i += 1

    if n_rows > 1:
        layout["grid"] = {"rows": n_rows, "columns": 1, "pattern": "independent"}
    if has_primary:
        layout["hovermode"] = "x unified"

    if len(bar_cols) >= 2:
        # The user's explicit choice wins. NOTE: Plotly's barmode is
        # figure-global, not per-subplot -- if a Histogram/Box/Violin
        # subplot is ALSO present at the same time, it shares this same
        # setting rather than always getting its own "overlay" below. A
        # rare combination in practice; documented rather than silently
        # doing the wrong thing.
        layout["barmode"] = config.bar_mode
    elif has_hist:
        # Multiple overlapping histograms read better semi-transparent
        # and overlaid than Plotly's default side-by-side "group" mode.
        layout["barmode"] = "overlay"

    return {"data": data, "layout": layout}


def _positioned(style: dict, domain: Optional[tuple], anchor: str) -> dict:
    if domain is None:
        return style
    return {**style, "domain": list(domain), "anchor": anchor}


def _stacked_domains(n: int, gap: float = 0.08) -> list:
    """``n`` equal-height [low, high] domains stacked top-to-bottom (the
    first entry is the topmost band), separated by ``gap`` fraction of
    the total height."""
    band = (1.0 - gap * (n - 1)) / n
    domains = []
    top = 1.0
    for _ in range(n):
        domains.append((top - band, top))
        top -= band + gap
    return domains


def _axis_style(
    palette: Palette, title: Optional[str] = None, accent: Optional[str] = None, log: bool = False
) -> dict:
    style = {
        "gridcolor": palette.grid_line,
        "zerolinecolor": palette.grid_line,
        "color": accent or palette.text,
        "linecolor": accent or palette.grid_line,
    }
    if title:
        style["title"] = {"text": title, "font": {"color": accent or palette.subtext}}
    if log:
        style["type"] = "log"
    return style


def _secondary_y_axis_style(
    palette: Palette, title: Optional[str], accent: Optional[str], log: bool = False
) -> dict:
    return {
        **_axis_style(palette, title, accent, log),
        "overlaying": "y",
        "side": "right",
        "anchor": "x",
        # The left axis' own gridlines already mark the plot's horizontal
        # scale; a second set at this axis' different scale would just
        # crisscross the first rather than add information.
        "showgrid": False,
    }


def _x_axis_style(palette: Palette, title: Optional[str] = None, log: bool = False) -> dict:
    return {
        **_axis_style(palette, title, log=log),
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


def build_map_plotly_spec(labels: list, config: MapConfig, arrays: dict, palette: Palette) -> dict:
    """Builds a lat/lon "Map" mode spec: a plain cartesian scatter with
    Longitude as X and Latitude as Y (an unprojected "Plate Carrée" grid)
    -- never Plotly's ``scattergeo``/mapbox subplot types. Both of those
    need to fetch map-outline/tile data from a CDN at render time even
    out of a "full" ``plotly.min.js`` bundle -- confirmed the hard way, a
    scattergeo chart tried (and, with no network reachable from inside
    QWebEngineView, failed) to fetch ``https://cdn.plot.ly/
    world_110m.json``. This app has no network dependency anywhere else,
    so Map mode can't have one either, with or without a custom basemap
    attached -- this cartesian approach is the only backend.

    The Y axis is locked to the X axis's scale (adjusted by latitude,
    since a degree of longitude covers less real distance away from the
    equator) so points -- and any attached raster basemap image -- aren't
    stretched. A raster basemap's image is cropped to just the plotted
    data's own area (``crop_raster_to_view``) before being anchored via
    ``layout.images``; a vector (GeoJSON) basemap's geometries are
    filtered the same way (``crop_vector_to_view``) before becoming plain
    line/fill traces on this same lon/lat grid -- a basemap file can
    cover a much larger area than the data ever does, and embedding it in
    full made pan/zoom sluggish (see core/basemap.py for both). With no
    basemap at all, this is just points/track on a plain gridded lon/lat
    plot -- a perfectly usable map on its own, just without geography
    drawn in.

    ``config.lat_lon_units`` ("degrees" or "radians") is converted to
    degrees immediately below, before anything else touches ``lat``/
    ``lon`` -- every axis title, aspect-ratio correction, and basemap
    crop computed further down assumes degrees.
    """
    lat = np.asarray(arrays[config.lat_column], dtype=float)
    lon = np.asarray(arrays[config.lon_column], dtype=float)
    if config.lat_lon_units == "radians":
        lat = np.degrees(lat)
        lon = np.degrees(lon)
    mode = "lines+markers" if config.connect_points else "markers"

    marker: dict = (
        _colorbar_marker(arrays[config.color_by], labels[config.color_by], palette)
        if config.color_by is not None
        else {"color": palette.accent}
    )
    if config.size_by is not None:
        marker["size"] = _bubble_sizes(arrays[config.size_by]).tolist()

    basemap = load_basemap(config.basemap_path) if config.basemap_path else None

    data = []
    if basemap is not None and basemap.kind == "vector":
        # Same reasoning as the raster crop below: a GeoJSON file can
        # cover a much larger area (e.g. a whole coastline) than the
        # plotted data does, so only the geometries actually near the
        # data get embedded/rendered rather than the entire file.
        if lon.size and lat.size:
            data = crop_vector_to_view(basemap, (float(lon.min()), float(lon.max())), (float(lat.min()), float(lat.max())))
        else:
            data = list(basemap.extra_traces)
    data.append(
        {
            "type": "scatter",
            "name": "Track",
            "x": lon.tolist(),
            "y": lat.tolist(),
            "mode": mode,
            "marker": marker,
            "line": {"color": palette.accent},
        }
    )

    mean_lat = float(np.mean(lat)) if lat.size else 0.0
    layout = _base_layout(palette)
    layout["xaxis"] = _x_axis_style(palette, "Longitude")
    layout["yaxis"] = {
        **_axis_style(palette, "Latitude"),
        "scaleanchor": "x",
        "scaleratio": max(abs(math.cos(math.radians(mean_lat))), 0.05),
    }
    if basemap is not None and basemap.kind == "raster":
        # Crop the (possibly much larger) basemap file down to just the
        # region this data actually covers before embedding it -- see
        # crop_raster_to_view for why: embedding the whole file made
        # pan/zoom sluggish for a basemap much bigger than the plotted
        # area.
        lon_view = (float(lon.min()), float(lon.max())) if lon.size else (basemap.lon_min, basemap.lon_max)
        lat_view = (float(lat.min()), float(lat.max())) if lat.size else (basemap.lat_min, basemap.lat_max)
        data_uri, img_lon_min, img_lon_max, img_lat_min, img_lat_max = crop_raster_to_view(
            basemap, lon_view, lat_view
        )
        layout["images"] = [
            {
                "source": data_uri,
                "xref": "x",
                "yref": "y",
                "x": img_lon_min,
                "y": img_lat_max,
                "sizex": img_lon_max - img_lon_min,
                "sizey": img_lat_max - img_lat_min,
                "xanchor": "left",
                "yanchor": "top",
                "sizing": "stretch",
                "layer": "below",
            }
        ]
    return {"data": data, "layout": layout}
