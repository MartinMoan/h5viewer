import json

import numpy as np
import pytest

from h5tools_app.core.h5_model import H5Model
from h5tools_app.core.plotting import (
    ChartType,
    GraphConfig,
    MapConfig,
    SeriesSpec,
    build_map_plotly_spec,
    build_plotly_spec,
    fetch_columns,
)
from h5tools_app.theme import Palette


def test_fetch_columns_matches_dataset(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/group1/matrix")
        layout = model.column_layout("/group1/matrix")
        arrays, truncated = fetch_columns(dataset, layout, [0, 2])
        assert not truncated
        expected = dataset[:]
        assert np.allclose(arrays[0], expected[:, 0])
        assert np.allclose(arrays[2], expected[:, 2])


def test_fetch_columns_truncates_and_flags(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/group1/linear")
        layout = model.column_layout("/group1/linear")
        arrays, truncated = fetch_columns(dataset, layout, [0], max_rows=100)
        assert truncated
        assert arrays[0].shape == (100,)
        assert list(arrays[0][:5]) == [0, 1, 2, 3, 4]


def test_fetch_columns_not_truncated_when_under_cap(sample_h5_path):
    with H5Model(sample_h5_path) as model:
        dataset = model.get_dataset("/group1/linear")
        layout = model.column_layout("/group1/linear")
        _, truncated = fetch_columns(dataset, layout, [0], max_rows=10_000)
        assert not truncated


def _palette():
    return Palette(dark=True)


def test_build_plotly_spec_trace_type_mode():
    arrays = {0: np.array([3.0, 1.0, 2.0]), 1: np.array([30.0, 10.0, 20.0]), 2: np.array([5.0, 6.0, 7.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE), 2: SeriesSpec(ChartType.SCATTER)})
    spec = build_plotly_spec(["x", "line_col", "scatter_col"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["line_col"]["type"] == "scatter"
    assert by_name["line_col"]["mode"] == "lines"
    # Plain SVG "scatter", not "scattergl" -- see _trace_type_mode for why
    # (WebGL rendering proved unreliable in this app's QWebEngineView).
    assert by_name["scatter_col"]["type"] == "scatter"
    assert by_name["scatter_col"]["mode"] == "markers"


def test_build_plotly_spec_bar_and_histogram_types():
    arrays = {0: np.array([1.0, 2.0, 3.0]), 1: np.array([1.0, 2.0, 3.0]), 2: np.array([1.0, 2.0, 3.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.BAR), 2: SeriesSpec(ChartType.HISTOGRAM)})
    spec = build_plotly_spec(["x", "bar_col", "hist_col"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["bar_col"]["type"] == "bar"
    assert by_name["hist_col"]["type"] == "histogram"


def test_build_plotly_spec_sorts_by_x():
    arrays = {0: np.array([3.0, 1.0, 2.0]), 1: np.array([30.0, 10.0, 20.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["x", "y"], config, arrays, _palette())
    trace = spec["data"][0]
    assert trace["x"] == [1.0, 2.0, 3.0]
    assert trace["y"] == [10.0, 20.0, 30.0]


def test_build_plotly_spec_series_color_matches_chart_color():
    # Not column_color(): that's the table's near-invisible background
    # tint (e.g. dark index 1 "#20262E" vs body_bg "#242424"), which made
    # an actual plotted line nearly invisible when reused directly.
    # chart_color() is a separate, vivid palette for this exact purpose.
    palette = _palette()
    arrays = {0: np.array([1.0, 2.0]), 3: np.array([1.0, 2.0])}
    config = GraphConfig(x_column=0, series={3: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["x", "b", "c", "y"], config, arrays, palette)
    trace = spec["data"][0]
    assert trace["line"]["color"] == palette.chart_color(3)
    assert trace["marker"]["color"] == palette.chart_color(3)
    assert trace["line"]["color"] != palette.column_color(3)


def test_build_plotly_spec_histogram_only_no_primary_axes():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([1.0, 2.0, 3.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.HISTOGRAM)})
    spec = build_plotly_spec(["x", "h"], config, arrays, _palette())
    assert "xaxis2" not in spec["layout"]
    assert spec["data"][0]["type"] == "histogram"


def test_build_plotly_spec_mixed_types_creates_second_axis_group():
    arrays = {0: np.array([1.0, 2.0, 3.0]), 1: np.array([1.0, 2.0, 3.0]), 2: np.array([1.0, 2.0, 3.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE), 2: SeriesSpec(ChartType.HISTOGRAM)})
    spec = build_plotly_spec(["x", "line_col", "hist_col"], config, arrays, _palette())
    assert "xaxis2" in spec["layout"]
    assert "yaxis2" in spec["layout"]
    assert spec["layout"]["hovermode"] == "x unified"


def test_build_plotly_spec_json_dumps_does_not_raise_with_nan():
    arrays = {0: np.array([1.0, float("nan"), 3.0]), 1: np.array([1.0, 2.0, float("nan")])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["x", "y"], config, arrays, _palette())
    # allow_nan=True (the default) emits bare NaN tokens -- valid JS source,
    # which is how this spec is actually embedded (see graph_window.py),
    # not valid strict JSON, but json.dumps itself must not raise.
    dumped = json.dumps(spec)
    assert "NaN" in dumped


def test_build_plotly_spec_axis_titles_single_series():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([3.0, 4.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["time", "temperature"], config, arrays, _palette())
    assert spec["layout"]["xaxis"]["title"]["text"] == "time"
    assert spec["layout"]["yaxis"]["title"]["text"] == "temperature"


def test_build_plotly_spec_axis_titles_untitled_when_multiple_series():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([3.0, 4.0]), 2: np.array([5.0, 6.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE), 2: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["time", "a", "b"], config, arrays, _palette())
    # X is unambiguous (one column) and still titled; Y has two series
    # sharing the axis, so it's left untitled -- disambiguated by the
    # legend/hover tooltip instead of a misleading single label.
    assert spec["layout"]["xaxis"]["title"]["text"] == "time"
    assert "title" not in spec["layout"]["yaxis"]


def test_build_plotly_spec_histogram_axis_titles():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([1.0, 2.0, 3.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.HISTOGRAM)})
    spec = build_plotly_spec(["x", "counts"], config, arrays, _palette())
    assert spec["layout"]["xaxis"]["title"]["text"] == "counts"
    assert spec["layout"]["yaxis"]["title"]["text"] == "Count"


def test_build_plotly_spec_right_axis_creates_yaxis3():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([20.0, 21.0]), 2: np.array([1000.0, 1010.0])}
    config = GraphConfig(
        x_column=0,
        series={1: SeriesSpec(ChartType.LINE, axis="left"), 2: SeriesSpec(ChartType.LINE, axis="right")},
    )
    spec = build_plotly_spec(["time", "temperature", "pressure"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["temperature"]["yaxis"] == "y"
    assert by_name["pressure"]["yaxis"] == "y3"
    assert spec["layout"]["yaxis3"]["overlaying"] == "y"
    assert spec["layout"]["yaxis3"]["side"] == "right"
    assert spec["layout"]["yaxis"]["title"]["text"] == "temperature"
    assert spec["layout"]["yaxis3"]["title"]["text"] == "pressure"


def test_build_plotly_spec_no_right_axis_series_omits_yaxis3():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([3.0, 4.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["x", "y"], config, arrays, _palette())
    assert "yaxis3" not in spec["layout"]
    assert spec["data"][0]["yaxis"] == "y"


def test_build_plotly_spec_right_axis_with_histogram_uses_distinct_axes():
    arrays = {0: np.array([1.0, 2.0, 3.0]), 1: np.array([1.0, 2.0, 3.0]), 2: np.array([1.0, 2.0, 3.0])}
    config = GraphConfig(
        x_column=0,
        series={1: SeriesSpec(ChartType.LINE, axis="right"), 2: SeriesSpec(ChartType.HISTOGRAM)},
    )
    spec = build_plotly_spec(["x", "line_col", "hist_col"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["line_col"]["yaxis"] == "y3"
    assert by_name["hist_col"]["yaxis"] == "y2"
    assert spec["layout"]["yaxis3"]["anchor"] == "x"


def test_build_plotly_spec_always_shows_legend():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([3.0, 4.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.LINE)})
    spec = build_plotly_spec(["x", "y"], config, arrays, _palette())
    assert spec["layout"]["showlegend"] is True


def test_build_plotly_spec_area_fills_to_zero():
    arrays = {0: np.array([1.0, 2.0, 3.0]), 1: np.array([3.0, 1.0, 2.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.AREA)})
    spec = build_plotly_spec(["x", "y"], config, arrays, _palette())
    trace = spec["data"][0]
    assert trace["type"] == "scatter" and trace["mode"] == "lines"
    assert trace["fill"] == "tozeroy"
    assert trace["fillcolor"].startswith("rgba(")


def test_build_plotly_spec_box_and_violin_share_a_subplot():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([1.0, 2.0, 3.0]), 2: np.array([4.0, 5.0, 6.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.BOX), 2: SeriesSpec(ChartType.VIOLIN)})
    spec = build_plotly_spec(["x", "box_col", "violin_col"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["box_col"]["type"] == "box"
    assert by_name["box_col"]["y"] == [1.0, 2.0, 3.0]
    assert by_name["violin_col"]["type"] == "violin"
    # No primary series -- alone, they take over the base (unnumbered)
    # axis pair, same as a lone Histogram would.
    assert by_name["box_col"]["xaxis"] == "x"
    assert by_name["violin_col"]["xaxis"] == "x"
    assert "xaxis2" not in spec["layout"]


def test_build_plotly_spec_primary_hist_and_boxviolin_get_distinct_axes():
    arrays = {col: np.array([1.0, 2.0, 3.0]) for col in range(4)}
    config = GraphConfig(
        x_column=0,
        series={
            1: SeriesSpec(ChartType.LINE),
            2: SeriesSpec(ChartType.HISTOGRAM),
            3: SeriesSpec(ChartType.BOX),
        },
    )
    spec = build_plotly_spec(["x", "line_col", "hist_col", "box_col"], config, arrays, _palette())
    by_name = {t["name"]: t for t in spec["data"]}
    # Primary keeps the base pair, Histogram gets slot 2, Box gets slot 4
    # -- 3 is reserved for the primary group's own right-axis overlay.
    assert by_name["line_col"]["xaxis"] == "x" and by_name["line_col"]["yaxis"] == "y"
    assert by_name["hist_col"]["xaxis"] == "x2" and by_name["hist_col"]["yaxis"] == "y2"
    assert by_name["box_col"]["xaxis"] == "x4" and by_name["box_col"]["yaxis"] == "y4"
    assert spec["layout"]["grid"] == {"rows": 3, "columns": 1, "pattern": "independent"}


def test_build_plotly_spec_scatter_color_and_size_by():
    arrays = {
        0: np.array([1.0, 2.0, 3.0]),
        1: np.array([10.0, 20.0, 30.0]),
        2: np.array([0.0, 5.0, 10.0]),
        3: np.array([1.0, 2.0, 3.0]),
    }
    config = GraphConfig(
        x_column=0, series={1: SeriesSpec(ChartType.SCATTER, color_by=2, size_by=3)}
    )
    spec = build_plotly_spec(["x", "y", "temp", "weight"], config, arrays, _palette())
    marker = spec["data"][0]["marker"]
    assert marker["color"] == [0.0, 5.0, 10.0]
    assert marker["colorscale"] == "Viridis"
    assert marker["colorbar"]["title"]["text"] == "weight" or marker["colorbar"]["title"]["text"] == "temp"
    # size_by is sqrt-area-scaled into a fixed px range, not a raw copy.
    assert len(marker["size"]) == 3
    assert min(marker["size"]) >= 6.0 and max(marker["size"]) <= 24.0
    assert marker["size"][0] < marker["size"][2]  # monotonic with the underlying values


def test_build_plotly_spec_bar_mode_defaults_to_group_and_honors_stack():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([1.0, 2.0]), 2: np.array([3.0, 4.0])}
    config = GraphConfig(x_column=0, series={1: SeriesSpec(ChartType.BAR), 2: SeriesSpec(ChartType.BAR)})
    spec = build_plotly_spec(["x", "a", "b"], config, arrays, _palette())
    assert spec["layout"]["barmode"] == "group"

    config = GraphConfig(
        x_column=0,
        series={1: SeriesSpec(ChartType.BAR), 2: SeriesSpec(ChartType.BAR)},
        bar_mode="stack",
    )
    spec = build_plotly_spec(["x", "a", "b"], config, arrays, _palette())
    assert spec["layout"]["barmode"] == "stack"


def test_build_plotly_spec_log_scale_axes():
    arrays = {0: np.array([1.0, 2.0]), 1: np.array([3.0, 4.0]), 2: np.array([300.0, 400.0])}
    config = GraphConfig(
        x_column=0,
        series={1: SeriesSpec(ChartType.LINE, axis="left"), 2: SeriesSpec(ChartType.LINE, axis="right")},
        log_x=True,
        log_y_left=True,
        log_y_right=True,
    )
    spec = build_plotly_spec(["x", "a", "b"], config, arrays, _palette())
    assert spec["layout"]["xaxis"]["type"] == "log"
    assert spec["layout"]["yaxis"]["type"] == "log"
    assert spec["layout"]["yaxis3"]["type"] == "log"


def test_graph_config_columns_used_includes_color_and_size_by():
    config = GraphConfig(
        x_column=0,
        series={1: SeriesSpec(ChartType.SCATTER, color_by=2, size_by=3), 4: SeriesSpec(ChartType.LINE)},
    )
    assert set(config.columns_used()) == {0, 1, 2, 3, 4}


def _map_arrays():
    return {
        0: np.array([10.0, 10.5, 11.0]),  # lat
        1: np.array([20.0, 20.5, 21.0]),  # lon
        2: np.array([1.0, 2.0, 3.0]),  # color-by
        3: np.array([1.0, 4.0, 9.0]),  # size-by
    }


def test_build_map_plotly_spec_converts_radians_to_degrees():
    import math

    arrays = {
        0: np.array([math.radians(10.0), math.radians(11.0)]),  # lat
        1: np.array([math.radians(20.0), math.radians(21.0)]),  # lon
    }
    config = MapConfig(lat_column=0, lon_column=1, lat_lon_units="radians")
    spec = build_map_plotly_spec(["lat", "lon"], config, arrays, _palette())
    x, y = spec["data"][0]["x"], spec["data"][0]["y"]
    assert x == pytest.approx([20.0, 21.0])
    assert y == pytest.approx([10.0, 11.0])


def test_build_map_plotly_spec_degrees_is_the_default_and_unchanged():
    config = MapConfig(lat_column=0, lon_column=1)
    assert config.lat_lon_units == "degrees"
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    assert spec["data"][0]["x"] == [20.0, 20.5, 21.0]
    assert spec["data"][0]["y"] == [10.0, 10.5, 11.0]


def test_build_map_plotly_spec_default_is_plain_cartesian_lon_lat():
    # Never scattergeo/mapbox -- both need a CDN fetch for map-outline/
    # tile data at render time even out of a "full" plotly.min.js
    # bundle, which breaks in this app's fully-offline QWebEngineView.
    config = MapConfig(lat_column=0, lon_column=1)
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    assert spec["data"][0]["type"] == "scatter"
    assert spec["data"][0]["mode"] == "markers"
    assert spec["data"][0]["x"] == [20.0, 20.5, 21.0]
    assert spec["data"][0]["y"] == [10.0, 10.5, 11.0]
    assert spec["layout"]["xaxis"]["title"]["text"] == "Longitude"
    assert spec["layout"]["yaxis"]["title"]["text"] == "Latitude"
    assert spec["layout"]["yaxis"]["scaleanchor"] == "x"
    assert "geo" not in spec["layout"]
    assert "images" not in spec["layout"]


def test_build_map_plotly_spec_connect_points_draws_lines():
    config = MapConfig(lat_column=0, lon_column=1, connect_points=True)
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    assert spec["data"][0]["mode"] == "lines+markers"


def test_build_map_plotly_spec_color_and_size_by():
    config = MapConfig(lat_column=0, lon_column=1, color_by=2, size_by=3)
    spec = build_map_plotly_spec(["lat", "lon", "depth", "weight"], config, _map_arrays(), _palette())
    marker = spec["data"][0]["marker"]
    assert marker["color"] == [1.0, 2.0, 3.0]
    assert marker["colorscale"] == "Viridis"
    assert len(marker["size"]) == 3


def test_build_map_plotly_spec_with_vector_geojson_basemap(tmp_path):
    geojson_path = tmp_path / "coast.geojson"
    geojson_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[20.0, 10.0], [21.0, 11.0]]}}
                ],
            }
        )
    )
    config = MapConfig(lat_column=0, lon_column=1, basemap_path=str(geojson_path))
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    # Same cartesian backend as always -- one extra trace for the
    # coastline plus the track itself, no images (that's raster-only).
    assert len(spec["data"]) == 2
    assert spec["data"][0]["type"] == "scatter"
    assert spec["data"][1]["type"] == "scatter"
    assert "images" not in spec["layout"]


def test_build_map_plotly_spec_filters_out_far_away_geojson_geometry(tmp_path):
    # A big GeoJSON file (e.g. a whole coastline) with one feature right
    # where the data is, and one feature nowhere near it -- only the
    # nearby one should make it into the spec; embedding the whole file
    # regardless of the data's own area was the reported performance bug.
    geojson_path = tmp_path / "world.geojson"
    geojson_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[20.0, 10.0], [21.0, 11.0]]},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[150.0, -40.0], [151.0, -41.0]]},
                    },
                ],
            }
        )
    )
    config = MapConfig(lat_column=0, lon_column=1, basemap_path=str(geojson_path))
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    # Just the nearby coastline segment plus the track -- not the
    # far-away one.
    assert len(spec["data"]) == 2
    assert spec["data"][0]["x"] == [20.0, 21.0]


def test_build_map_plotly_spec_with_raster_basemap_uses_cartesian_backend(tmp_path):
    from PIL import Image

    img_path = tmp_path / "chart.png"
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(img_path)
    world_path = tmp_path / "chart.pgw"
    # 6-line affine world file: pixel size x, rotation, rotation, pixel
    # size y (negative -- north-up), top-left X, top-left Y.
    world_path.write_text("0.01\n0\n0\n-0.01\n20.0\n11.0\n")

    config = MapConfig(lat_column=0, lon_column=1, basemap_path=str(img_path))
    spec = build_map_plotly_spec(["lat", "lon"], config, _map_arrays(), _palette())
    assert spec["data"][0]["type"] == "scatter"
    assert spec["data"][0]["x"] == [20.0, 20.5, 21.0]
    assert spec["layout"]["yaxis"]["scaleanchor"] == "x"
    image = spec["layout"]["images"][0]
    assert image["source"].startswith("data:image/png;base64,")
    assert image["x"] == 20.0
    assert image["y"] == 11.0
