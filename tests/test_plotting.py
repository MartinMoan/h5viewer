import json

import numpy as np

from h5tools_app.core.h5_model import H5Model
from h5tools_app.core.plotting import ChartType, GraphConfig, SeriesSpec, build_plotly_spec, fetch_columns
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
