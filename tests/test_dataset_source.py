import time

import h5py
import numpy as np
import pytest

from h5tools_app.core.dataset_source import DatasetSource
from h5tools_app.core.h5_model import build_column_layout


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def open_dataset(sample_h5_path):
    f = h5py.File(sample_h5_path, "r")
    yield f
    f.close()


def test_get_available_returns_none_until_loaded(open_dataset):
    dataset = open_dataset["/group1/linear"]
    layout = build_column_layout(dataset.shape, dataset.dtype)
    source = DatasetSource(dataset, layout, block_size=50)
    try:
        rows, missing = source.get_available(0, 10)
        assert rows is None
        assert missing == [0]

        source.ensure_loaded(0, 10)
        assert _wait_until(lambda: source.get_available(0, 10)[0] is not None)

        rows, missing = source.get_available(0, 10)
        assert missing == []
        assert [rows[i, 0] for i in range(10)] == list(range(10))
    finally:
        source.close()


def test_cached_blocks_reused_across_requests(open_dataset):
    dataset = open_dataset["/group1/linear"]
    layout = build_column_layout(dataset.shape, dataset.dtype)
    source = DatasetSource(dataset, layout, block_size=100)
    try:
        source.ensure_loaded(0, 50)
        assert _wait_until(lambda: source.get_available(0, 50)[0] is not None)

        # Second, overlapping request for data in the same block should be
        # served entirely from cache -- no missing blocks.
        rows, missing = source.get_available(20, 80)
        assert missing == []
        assert [rows[i, 0] for i in range(60)] == list(range(20, 80))
    finally:
        source.close()


def test_spans_multiple_blocks(open_dataset):
    dataset = open_dataset["/group1/linear"]
    layout = build_column_layout(dataset.shape, dataset.dtype)
    source = DatasetSource(dataset, layout, block_size=30)
    try:
        source.ensure_loaded(10, 90)
        assert _wait_until(lambda: source.get_available(10, 90)[0] is not None, timeout=5)
        rows, missing = source.get_available(10, 90)
        assert missing == []
        assert [rows[i, 0] for i in range(80)] == list(range(10, 90))
    finally:
        source.close()


def test_poll_updates_reports_finished_blocks(open_dataset):
    dataset = open_dataset["/group1/linear"]
    layout = build_column_layout(dataset.shape, dataset.dtype)
    source = DatasetSource(dataset, layout, block_size=100)
    try:
        source.ensure_loaded(0, 10)
        assert _wait_until(lambda: source.poll_updates() or source.get_available(0, 10)[0] is not None)
        # by now block 0 should be cached even if we already drained the queue
        rows, missing = source.get_available(0, 10)
        assert missing == []
    finally:
        source.close()


def test_matrix_dataset_rows(open_dataset):
    dataset = open_dataset["/group1/matrix"]
    layout = build_column_layout(dataset.shape, dataset.dtype)
    source = DatasetSource(dataset, layout, block_size=10)
    try:
        source.ensure_loaded(0, 20)
        assert _wait_until(lambda: source.get_available(0, 20)[0] is not None)
        rows, _ = source.get_available(0, 20)
        expected = dataset[0:20]
        for r in range(20):
            for col in range(4):
                assert rows[r, col] == expected[r, col]
    finally:
        source.close()
