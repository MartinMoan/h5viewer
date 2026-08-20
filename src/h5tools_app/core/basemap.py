"""Loads a user-supplied offline basemap file for the graph "Map" mode
(see ``core/plotting.build_map_plotly_spec``). Two kinds of file are
supported:

* Vector (``.geojson``/``.json``) -- parsed with the stdlib ``json``
  module and turned into extra Plotly trace dicts (plain cartesian lon/
  lat line/fill traces, not ``scattergeo`` -- see
  ``core/plotting.build_map_plotly_spec`` for why), e.g. a coastline or
  reference boundary. No image involved.
* Raster (``.png``/``.jpg``/``.jpeg``/``.tif``/``.tiff``) -- a plain
  image, georeferenced either by a sidecar "world file"
  (``.pgw``/``.jgw``/``.tfw``/``.wld`` -- a 6-line affine transform, the
  common GDAL-free way to georeference an image) or, for ``.tif``/
  ``.tiff`` without one, by reading GeoTIFF's own embedded
  ModelPixelScale/ModelTiepoint tags directly via Pillow (covers the
  common north-up, unrotated, plain lat/lon case with zero extra
  dependencies).

Deliberately dependency-light (stdlib ``json`` + the Pillow this app
already uses for ``icons.py``) rather than pulling in GDAL/rasterio/
pyproj, matching this codebase's general aversion to heavy/fragile
dependencies (see e.g. the scattergl-avoidance note in
``core/plotting._trace_type_mode``). Arbitrary-CRS reprojection and
formats like BSB/KAP or Shapefile are explicitly out of scope -- see the
graphing plan.
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
_WORLD_FILE_EXTS = {
    ".png": ".pgw",
    ".jpg": ".jgw",
    ".jpeg": ".jgw",
    ".tif": ".tfw",
    ".tiff": ".tfw",
}
# GeoTIFF tags (OGC GeoTIFF spec) -- read directly since Pillow exposes
# raw TIFF tags without needing any GDAL-style GeoTIFF support.
_TAG_MODEL_PIXEL_SCALE = 33550
_TAG_MODEL_TIEPOINT = 33922


class BasemapError(ValueError):
    """A basemap file couldn't be loaded -- message is user-facing."""


@dataclass(frozen=True)
class BasemapResult:
    kind: str  # "raster" or "vector"
    # Raster only -- kept as a loaded PIL Image rather than an
    # already-encoded data URI, so the caller can crop it down to just
    # the region actually being plotted (see crop_raster_to_view) before
    # paying to encode/embed/render it. A basemap file can cover a much
    # larger area than the data ever does, and embedding the whole thing
    # at full resolution was what made pan/zoom sluggish.
    image: Optional["Image.Image"] = None
    lon_min: Optional[float] = None
    lon_max: Optional[float] = None
    lat_min: Optional[float] = None
    lat_max: Optional[float] = None
    extra_traces: list = field(default_factory=list)


def load_basemap(path: str) -> BasemapResult:
    ext = Path(path).suffix.lower()
    if ext in (".geojson", ".json"):
        return _load_vector(path)
    if ext in _RASTER_EXTS:
        return _load_raster(path, ext)
    raise BasemapError(
        f"Unsupported basemap file type '{ext}'. Supported: GeoJSON (.geojson), "
        "or a georeferenced image -- PNG/JPEG/TIFF with a matching .pgw/.jgw/.tfw/.wld "
        "world file, or a GeoTIFF with embedded georeferencing tags."
    )


def _load_vector(path: str) -> BasemapResult:
    with open(path, "r", encoding="utf-8") as f:
        geojson = json.load(f)

    traces: list = []

    def walk(geometry: dict) -> None:
        gtype = geometry.get("type")
        coords = geometry.get("coordinates")
        if gtype == "Point":
            traces.append(
                {
                    "type": "scatter",
                    "mode": "markers",
                    "x": [coords[0]],
                    "y": [coords[1]],
                    "showlegend": False,
                    "name": "",
                }
            )
        elif gtype == "LineString":
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": [c[0] for c in coords],
                    "y": [c[1] for c in coords],
                    "showlegend": False,
                    "name": "",
                }
            )
        elif gtype == "Polygon":
            for ring in coords:
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "fill": "toself",
                        "x": [c[0] for c in ring],
                        "y": [c[1] for c in ring],
                        "showlegend": False,
                        "name": "",
                    }
                )
        elif gtype in ("MultiPoint", "MultiLineString", "MultiPolygon"):
            single = gtype[len("Multi") :]
            for part in coords:
                walk({"type": single, "coordinates": part})
        elif gtype == "GeometryCollection":
            for geom in geometry.get("geometries", []):
                walk(geom)

    features = geojson.get("features", [geojson]) if geojson.get("type") == "FeatureCollection" else [geojson]
    for feature in features:
        geometry = feature.get("geometry", feature)
        if geometry:
            walk(geometry)

    return BasemapResult(kind="vector", extra_traces=traces)


def crop_vector_to_view(basemap: BasemapResult, lon_range: tuple, lat_range: tuple, pad_frac: float = 0.2) -> list:
    """Returns just the subset of ``basemap.extra_traces`` whose own
    bounding box intersects the (padded) lon/lat view -- so a large
    GeoJSON file (e.g. a whole coastline made of many segments) doesn't
    get embedded and re-rendered in full on every pan/zoom when the
    plotted data only covers a small regional area, the same problem
    ``crop_raster_to_view`` solves for a raster basemap.

    A geometry that only *partially* overlaps the view (e.g. one very
    long segment passing through the region) is kept whole rather than
    clipped to just the visible portion -- real-world boundary/coastline
    files are typically split into many smaller features already, so
    this bounding-box filter alone removes the vast majority of
    off-screen data without needing true polyline/polygon clipping.
    """
    lon_min, lon_max = lon_range
    lat_min, lat_max = lat_range
    lon_pad = max((lon_max - lon_min) * pad_frac, 0.01)
    lat_pad = max((lat_max - lat_min) * pad_frac, 0.01)
    view_lon = (lon_min - lon_pad, lon_max + lon_pad)
    view_lat = (lat_min - lat_pad, lat_max + lat_pad)

    kept = []
    for trace in basemap.extra_traces:
        xs, ys = trace.get("x"), trace.get("y")
        if not xs or not ys:
            continue
        if max(xs) < view_lon[0] or min(xs) > view_lon[1]:
            continue
        if max(ys) < view_lat[0] or min(ys) > view_lat[1]:
            continue
        kept.append(trace)
    return kept


def _load_raster(path: str, ext: str) -> BasemapResult:
    world_path = Path(path).with_suffix(_WORLD_FILE_EXTS[ext])
    if world_path.exists():
        bounds = _read_world_file(world_path, path)
    elif ext in (".tif", ".tiff"):
        bounds = _read_geotiff_bounds(path)
    else:
        raise BasemapError(
            f"No matching world file found ({world_path.name}) -- a PNG/JPEG basemap needs one "
            "alongside it to be georeferenced. TIFF files can alternatively carry their own "
            "embedded GeoTIFF georeferencing tags."
        )

    img = Image.open(path)
    img.load()  # force the read now -- the file handle doesn't need to stay open

    lon_min, lon_max, lat_min, lat_max = bounds
    return BasemapResult(
        kind="raster", image=img, lon_min=lon_min, lon_max=lon_max, lat_min=lat_min, lat_max=lat_max
    )


def crop_raster_to_view(
    basemap: BasemapResult, lon_range: tuple, lat_range: tuple, pad_frac: float = 0.2
) -> tuple:
    """Crops ``basemap.image`` down to just the region covering
    ``lon_range``/``lat_range`` (the data actually being plotted), padded
    by ``pad_frac`` of that region's own span (or a small slice of the
    full image's span, whichever is bigger -- so a near-zero-span
    selection, e.g. a single point, still gets a sensibly-sized crop
    rather than a sliver), then encodes just that crop to a PNG data
    URI. Falls back to the whole image if the requested range doesn't
    overlap it at all.

    A basemap file can cover a much larger area than the plotted data
    ever does -- embedding the *whole* file as one Plotly background
    image meant the browser was decoding/rescaling a huge bitmap on
    every pan/zoom, which is what made those interactions sluggish.
    Cropping to the region that's actually in view keeps the embedded
    image proportional to the data instead.
    """
    full_lon_span = basemap.lon_max - basemap.lon_min
    full_lat_span = basemap.lat_max - basemap.lat_min

    req_lon_min, req_lon_max = lon_range
    req_lat_min, req_lat_max = lat_range
    lon_pad = max((req_lon_max - req_lon_min) * pad_frac, full_lon_span * 0.02)
    lat_pad = max((req_lat_max - req_lat_min) * pad_frac, full_lat_span * 0.02)
    lon_min = max(req_lon_min - lon_pad, basemap.lon_min)
    lon_max = min(req_lon_max + lon_pad, basemap.lon_max)
    lat_min = max(req_lat_min - lat_pad, basemap.lat_min)
    lat_max = min(req_lat_max + lat_pad, basemap.lat_max)

    width, height = basemap.image.size
    cropped = None
    if lon_min < lon_max and lat_min < lat_max and full_lon_span and full_lat_span:
        left = max(0, int((lon_min - basemap.lon_min) / full_lon_span * width))
        right = min(width, int((lon_max - basemap.lon_min) / full_lon_span * width))
        # Image row 0 is the top (north / lat_max), so higher latitude
        # means a smaller row index.
        top = max(0, int((basemap.lat_max - lat_max) / full_lat_span * height))
        bottom = min(height, int((basemap.lat_max - lat_min) / full_lat_span * height))
        if right > left and bottom > top:
            cropped = basemap.image.crop((left, top, right, bottom))

    if cropped is None:
        # The requested view doesn't usefully overlap the image (or fell
        # entirely outside it after clamping) -- fall back to the whole
        # thing rather than an inverted/empty crop.
        cropped = basemap.image
        lon_min, lon_max, lat_min, lat_max = basemap.lon_min, basemap.lon_max, basemap.lat_min, basemap.lat_max

    buf = io.BytesIO()
    cropped.convert("RGBA").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return data_uri, lon_min, lon_max, lat_min, lat_max


def _read_world_file(world_path: Path, image_path: str) -> tuple:
    with open(world_path, "r", encoding="utf-8") as f:
        values = [float(line.strip()) for line in f if line.strip()]
    if len(values) != 6:
        raise BasemapError(f"'{world_path.name}' doesn't look like a valid world file (expected 6 lines).")
    px_size_x, _rot1, _rot2, px_size_y, origin_x, origin_y = values

    with Image.open(image_path) as img:
        width, height = img.size

    lon_min = origin_x
    lon_max = origin_x + width * px_size_x
    lat_max = origin_y
    lat_min = origin_y + height * px_size_y  # px_size_y is negative for a north-up image
    return (min(lon_min, lon_max), max(lon_min, lon_max), min(lat_min, lat_max), max(lat_min, lat_max))


def _read_geotiff_bounds(path: str) -> tuple:
    with Image.open(path) as img:
        tags = getattr(img, "tag_v2", None)
        width, height = img.size
        if tags is None or _TAG_MODEL_PIXEL_SCALE not in tags or _TAG_MODEL_TIEPOINT not in tags:
            raise BasemapError(
                f"'{Path(path).name}' has no matching world file and no embedded GeoTIFF "
                "georeferencing tags -- add a .tfw world file alongside it to use it as a basemap."
            )
        scale = tags[_TAG_MODEL_PIXEL_SCALE]
        tiepoint = tags[_TAG_MODEL_TIEPOINT]
        px_scale_x, px_scale_y = float(scale[0]), float(scale[1])
        # Tiepoint is (pixel_x, pixel_y, pixel_z, model_x, model_y, model_z);
        # the common case has the first tiepoint at pixel (0, 0).
        model_x, model_y = float(tiepoint[3]), float(tiepoint[4])

    lon_min = model_x
    lon_max = model_x + width * px_scale_x
    lat_max = model_y
    lat_min = model_y - height * px_scale_y
    return (min(lon_min, lon_max), max(lon_min, lon_max), min(lat_min, lat_max), max(lat_min, lat_max))
