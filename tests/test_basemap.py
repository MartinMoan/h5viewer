import json

import pytest
from PIL import Image

from h5tools_app.core.basemap import BasemapError, crop_raster_to_view, crop_vector_to_view, load_basemap


def test_load_basemap_unsupported_extension_raises_helpful_error(tmp_path):
    path = tmp_path / "chart.shp"
    path.write_bytes(b"not really a shapefile")
    with pytest.raises(BasemapError, match="Unsupported basemap file type"):
        load_basemap(str(path))


def test_load_basemap_geojson_linestring_becomes_cartesian_trace(tmp_path):
    path = tmp_path / "coast.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[1.0, 2.0], [3.0, 4.0]]},
                    }
                ],
            }
        )
    )
    result = load_basemap(str(path))
    assert result.kind == "vector"
    assert len(result.extra_traces) == 1
    trace = result.extra_traces[0]
    assert trace["type"] == "scatter"
    assert trace["x"] == [1.0, 3.0]
    assert trace["y"] == [2.0, 4.0]


def test_crop_vector_to_view_drops_geometries_outside_the_padded_view(tmp_path):
    path = tmp_path / "world.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[100.0, 50.0], [101.0, 51.0]]},
                    },
                ],
            }
        )
    )
    basemap = load_basemap(str(path))
    assert len(basemap.extra_traces) == 2

    kept = crop_vector_to_view(basemap, lon_range=(0.0, 1.0), lat_range=(0.0, 1.0))
    assert len(kept) == 1
    assert kept[0]["x"] == [0.0, 1.0]


def test_load_basemap_geojson_polygon_and_multi_geometry(tmp_path):
    path = tmp_path / "areas.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.0, 0.0]]],
                        },
                    },
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "MultiPoint",
                            "coordinates": [[5.0, 6.0], [7.0, 8.0]],
                        },
                    },
                ],
            }
        )
    )
    result = load_basemap(str(path))
    assert result.kind == "vector"
    # 1 polygon ring trace + 2 individual points from the MultiPoint.
    assert len(result.extra_traces) == 3
    kinds = [t.get("fill") for t in result.extra_traces]
    assert "toself" in kinds


def test_load_basemap_raster_with_world_file(tmp_path):
    img_path = tmp_path / "chart.png"
    Image.new("RGB", (10, 5), color=(1, 2, 3)).save(img_path)
    world_path = tmp_path / "chart.pgw"
    world_path.write_text("0.1\n0\n0\n-0.2\n30.0\n50.0\n")

    result = load_basemap(str(img_path))
    assert result.kind == "raster"
    assert result.image is not None and result.image.size == (10, 5)
    # lon: origin 30.0, width 10 * pixel size 0.1 => 31.0
    assert result.lon_min == pytest.approx(30.0)
    assert result.lon_max == pytest.approx(31.0)
    # lat: origin (top) 50.0, height 5 * pixel size -0.2 => bottom 49.0
    assert result.lat_min == pytest.approx(49.0)
    assert result.lat_max == pytest.approx(50.0)


def test_crop_raster_to_view_shrinks_to_a_small_data_region(tmp_path):
    # A big 100x100 basemap spanning 10 degrees each way, but the data
    # only occupies a tiny corner of it -- the crop should come back
    # much smaller than the full image, not the whole 100x100 file.
    img_path = tmp_path / "big.png"
    Image.new("RGB", (100, 100), color=(5, 5, 5)).save(img_path)
    world_path = tmp_path / "big.pgw"
    world_path.write_text("0.1\n0\n0\n-0.1\n0.0\n10.0\n")  # covers lon[0,10], lat[0,10]

    basemap = load_basemap(str(img_path))
    data_uri, lon_min, lon_max, lat_min, lat_max = crop_raster_to_view(
        basemap, lon_range=(0.0, 0.5), lat_range=(9.5, 10.0)
    )
    assert data_uri.startswith("data:image/png;base64,")
    # Cropped bounds should stay well within the full 10-degree extent.
    assert lon_max - lon_min < 5.0
    assert lat_max - lat_min < 5.0
    assert lon_min >= 0.0 and lon_max <= 10.0
    assert lat_min >= 0.0 and lat_max <= 10.0


def test_crop_raster_to_view_falls_back_to_whole_image_outside_bounds(tmp_path):
    img_path = tmp_path / "small.png"
    Image.new("RGB", (4, 4)).save(img_path)
    world_path = tmp_path / "small.pgw"
    world_path.write_text("1\n0\n0\n-1\n0.0\n4.0\n")  # covers lon[0,4], lat[0,4]

    basemap = load_basemap(str(img_path))
    # A view range entirely outside the image's own extent should still
    # return something sane (the whole image) rather than an empty crop.
    _data_uri, lon_min, lon_max, lat_min, lat_max = crop_raster_to_view(
        basemap, lon_range=(100.0, 101.0), lat_range=(100.0, 101.0)
    )
    assert (lon_min, lon_max, lat_min, lat_max) == (basemap.lon_min, basemap.lon_max, basemap.lat_min, basemap.lat_max)


def test_load_basemap_raster_without_world_file_raises_helpful_error(tmp_path):
    img_path = tmp_path / "chart.png"
    Image.new("RGB", (2, 2)).save(img_path)
    with pytest.raises(BasemapError, match="No matching world file"):
        load_basemap(str(img_path))


def test_load_basemap_tiff_without_world_file_or_geotags_raises_helpful_error(tmp_path):
    img_path = tmp_path / "chart.tif"
    Image.new("RGB", (2, 2)).save(img_path)
    with pytest.raises(BasemapError, match="no embedded GeoTIFF"):
        load_basemap(str(img_path))


def test_load_basemap_world_file_malformed_raises(tmp_path):
    img_path = tmp_path / "chart.png"
    Image.new("RGB", (2, 2)).save(img_path)
    world_path = tmp_path / "chart.pgw"
    world_path.write_text("0.1\n0\n0\n")  # only 3 lines, not 6
    with pytest.raises(BasemapError, match="doesn't look like a valid world file"):
        load_basemap(str(img_path))
