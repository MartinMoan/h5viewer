"""Structural navigation over an HDF5 file.

This module knows how to walk an ``h5py.File`` lazily (only touching
metadata, never dataset contents) and how to describe a dataset's shape
as a flat table of columns so it can be rendered in a grid. Reading the
actual row data lives in :mod:`dataset_source`, which uses the
``read_rows`` helper defined here.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np

from ..constants import MAX_COLUMNS

GROUP = "group"
DATASET = "dataset"


@dataclass(frozen=True)
class NodeInfo:
    path: str
    name: str
    kind: str  # GROUP or DATASET
    shape: tuple | None = None
    dtype: str | None = None
    n_children: int | None = None  # groups only


@dataclass(frozen=True)
class ColumnLayout:
    """Describes how an N-D / compound dataset maps onto a 2D table."""

    row_count: int
    labels: list[str]
    total_columns: int
    truncated: bool
    field_names: tuple | None
    trailing_shape: tuple
    is_scalar: bool
    # Parallel to labels: whether each column's dtype is int/uint/float
    # (explicitly excludes bool and string/object columns) -- used to
    # decide which columns are offered for plotting.
    numeric_mask: tuple[bool, ...]

    @property
    def n_columns(self) -> int:
        return len(self.labels)

    def numeric_columns(self) -> list[int]:
        return [i for i, numeric in enumerate(self.numeric_mask) if numeric]


class H5ModelError(RuntimeError):
    pass


class H5Model:
    """Thin, lazy wrapper around a single open HDF5 file.

    Not thread-safe by itself: callers must ensure only one thread touches
    the underlying ``h5py.File`` at a time (the app funnels dataset reads
    through a single worker thread per open file, see ``dataset_source``).
    """

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        self.file = h5py.File(self.path, "r")

    def close(self) -> None:
        try:
            self.file.close()
        except Exception:
            pass

    def __enter__(self) -> "H5Model":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _resolve(self, path: str):
        path = path or "/"
        if path == "/":
            return self.file
        return self.file[path]

    def root_info(self) -> NodeInfo:
        return self.node_info("/")

    def node_info(self, path: str) -> NodeInfo:
        obj = self._resolve(path)
        name = "/" if path in ("", "/") else path.rsplit("/", 1)[-1]
        if isinstance(obj, h5py.Dataset):
            return NodeInfo(
                path=path,
                name=name,
                kind=DATASET,
                shape=obj.shape,
                dtype=str(obj.dtype),
            )
        return NodeInfo(
            path=path,
            name=name,
            kind=GROUP,
            n_children=len(obj.keys()),
        )

    def list_children(self, path: str) -> list[NodeInfo]:
        """Return immediate children of a group, groups first, then
        datasets, both alphabetically -- mirrors a typical file explorer.
        """
        obj = self._resolve(path)
        if not isinstance(obj, h5py.Group):
            return []

        groups, datasets = [], []
        for key in obj.keys():
            try:
                # Skip broken external/soft links rather than raising.
                target = obj[key]
            except (KeyError, OSError):
                continue
            child_path = f"{path.rstrip('/')}/{key}" if path != "/" else f"/{key}"
            if isinstance(target, h5py.Dataset):
                datasets.append(
                    NodeInfo(
                        path=child_path,
                        name=key,
                        kind=DATASET,
                        shape=target.shape,
                        dtype=str(target.dtype),
                    )
                )
            elif isinstance(target, h5py.Group):
                groups.append(
                    NodeInfo(
                        path=child_path,
                        name=key,
                        kind=GROUP,
                        n_children=len(target.keys()),
                    )
                )
        groups.sort(key=lambda n: n.name.lower())
        datasets.sort(key=lambda n: n.name.lower())
        return groups + datasets

    def get_attrs(self, path: str) -> dict:
        obj = self._resolve(path)
        out = {}
        for key, value in obj.attrs.items():
            out[key] = _format_attr_value(value)
        return out

    def get_dataset(self, path: str) -> h5py.Dataset:
        obj = self._resolve(path)
        if not isinstance(obj, h5py.Dataset):
            raise H5ModelError(f"{path!r} is not a dataset")
        return obj

    def column_layout(self, path: str, max_columns: int = MAX_COLUMNS) -> ColumnLayout:
        dataset = self.get_dataset(path)
        return build_column_layout(dataset.shape, dataset.dtype, max_columns)


def _format_attr_value(value) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, np.ndarray):
        if value.size <= 12:
            return np.array2string(value, threshold=12)
        return f"<array shape={value.shape} dtype={value.dtype}>"
    return str(value)


def _is_numeric_kind(kind: str) -> bool:
    # int, unsigned int, float -- explicitly not bool ("b") or
    # string/bytes/object ("U", "S", "O"), which aren't plottable.
    return kind in "iuf"


def build_column_layout(shape: tuple, dtype: np.dtype, max_columns: int = MAX_COLUMNS) -> ColumnLayout:
    field_names = dtype.names
    is_scalar = len(shape) == 0

    if is_scalar:
        row_count = 1
        trailing_shape = ()
    else:
        row_count = shape[0]
        trailing_shape = shape[1:]

    if field_names:
        n_fields = len(field_names)
    else:
        n_fields = 1

    trailing_combos = 1
    for dim in trailing_shape:
        trailing_combos *= max(dim, 1)

    total_columns = trailing_combos * n_fields
    truncated = total_columns > max_columns

    labels: list[str] = []
    numeric_mask: list[bool] = []
    if not trailing_shape:
        if field_names:
            labels = list(field_names[:max_columns])
            numeric_mask = [_is_numeric_kind(dtype[f].kind) for f in labels]
        else:
            labels = ["value"]
            numeric_mask = [_is_numeric_kind(dtype.kind)]
    else:
        ranges = [range(d) for d in trailing_shape]
        count = 0
        for idx in itertools.product(*ranges):
            idx_label = ",".join(str(i) for i in idx)
            if field_names:
                for f in field_names:
                    labels.append(f"[{idx_label}].{f}")
                    numeric_mask.append(_is_numeric_kind(dtype[f].kind))
                    count += 1
                    if count >= max_columns:
                        break
            else:
                labels.append(f"[{idx_label}]")
                numeric_mask.append(_is_numeric_kind(dtype.kind))
                count += 1
            if count >= max_columns:
                break

    return ColumnLayout(
        row_count=row_count,
        labels=labels,
        total_columns=total_columns,
        truncated=truncated,
        field_names=field_names,
        trailing_shape=trailing_shape,
        is_scalar=is_scalar,
        numeric_mask=tuple(numeric_mask),
    )


def read_rows(dataset: h5py.Dataset, start: int, end: int, layout: ColumnLayout) -> np.ndarray:
    """Read ``[start, end)`` rows from ``dataset`` and reshape them to
    match ``layout`` -- one row per original leading-axis index, one
    column per flattened trailing-axis/field combination.

    Returns an ``object`` ndarray of shape ``(end - start, layout.n_columns)``
    so heterogeneous / compound values can sit next to plain scalars.
    """
    if layout.is_scalar:
        chunk = dataset[()]
        rows = [chunk]
    else:
        chunk = dataset[start:end]
        rows = list(chunk)

    n_rows = len(rows)
    n_cols = layout.n_columns
    out = np.empty((n_rows, n_cols), dtype=object)

    for r, row in enumerate(rows):
        if not layout.trailing_shape:
            if layout.field_names:
                for c, f in enumerate(layout.field_names[:n_cols]):
                    out[r, c] = row[f]
            else:
                out[r, 0] = row
        else:
            ranges = [range(d) for d in layout.trailing_shape]
            c = 0
            for idx in itertools.product(*ranges):
                cell = row[idx]
                if layout.field_names:
                    for f in layout.field_names:
                        if c >= n_cols:
                            break
                        out[r, c] = cell[f]
                        c += 1
                else:
                    if c >= n_cols:
                        break
                    out[r, c] = cell
                    c += 1
                if c >= n_cols:
                    break
    return out
