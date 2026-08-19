"""Background, chunked, cached reader for a single HDF5 dataset.

The dataset table widget never touches h5py directly. Instead it asks a
``DatasetSource`` for the rows currently on screen. Reads happen in fixed
size "blocks" of rows, on a single dedicated worker thread (h5py file
handles are not safe to hit concurrently from multiple threads), and
completed blocks are cached so that scrolling back over already-seen data
is instant. This is what lets the table view scroll continuously over a
dataset far larger than memory: only the handful of blocks near the
current viewport are ever resident.

This module is intentionally free of any Tkinter dependency so it can be
unit tested in isolation -- the widget layer polls ``poll_updates`` from
its own ``after()`` loop to find out when new data has arrived.
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque
from queue import Empty, Queue

import h5py
import numpy as np

from ..constants import MAX_CACHED_BLOCKS, ROW_BLOCK_SIZE
from .h5_model import ColumnLayout, read_rows

# Upper bound on how many not-yet-started block requests we let build up.
# Bounds worst-case latency after a very fast scroll: we'd rather drop a
# stale request than make the user wait for blocks they've already
# scrolled past.
MAX_QUEUED_REQUESTS = 48


class DatasetSource:
    def __init__(
        self,
        dataset: h5py.Dataset,
        layout: ColumnLayout,
        block_size: int = ROW_BLOCK_SIZE,
        max_cached_blocks: int = MAX_CACHED_BLOCKS,
    ):
        self._dataset = dataset
        self.layout = layout
        self.row_count = layout.row_count
        self.block_size = block_size
        self.n_blocks = max(1, (self.row_count + block_size - 1) // block_size)
        self._max_cached_blocks = max_cached_blocks

        self._cache: "OrderedDict[int, np.ndarray]" = OrderedDict()
        self._cache_lock = threading.Lock()

        self._work: deque = deque()  # block indices, most-recently-requested first
        self._queued_set: set = set()
        self._inflight = None
        self._cv = threading.Condition()
        self._closed = False

        self._result_queue: "Queue[int]" = Queue()

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    @property
    def dataset(self) -> h5py.Dataset:
        """The underlying h5py.Dataset -- for one-shot bulk reads that
        bypass the block cache entirely (see core/plotting.py)."""
        return self._dataset

    # -- public API ---------------------------------------------------

    def get_available(self, start: int, end: int):
        """Return ``(rows, missing_blocks)``.

        ``rows`` is an ``(end-start, n_columns)`` object ndarray if every
        block covering the range is already cached, else ``None``.
        ``missing_blocks`` lists the block indices that still need to be
        loaded (empty when ``rows`` is not None).
        """
        start = max(0, start)
        end = min(self.row_count, end)
        if start >= end:
            return np.empty((0, self.layout.n_columns), dtype=object), []

        first_block = start // self.block_size
        last_block = (end - 1) // self.block_size

        with self._cache_lock:
            missing = [b for b in range(first_block, last_block + 1) if b not in self._cache]
            if missing:
                return None, missing
            parts = [self._cache[b] for b in range(first_block, last_block + 1)]
            for b in range(first_block, last_block + 1):
                self._cache.move_to_end(b)

        full = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
        lo = start - first_block * self.block_size
        hi = lo + (end - start)
        return full[lo:hi], []

    def ensure_loaded(self, start: int, end: int, prefetch: int = 1) -> None:
        """Schedule background loads for blocks covering ``[start, end)``
        plus ``prefetch`` extra blocks on either side, prioritized so the
        most recently requested range loads first.
        """
        start = max(0, start)
        end = min(self.row_count, end)
        if start >= end:
            return

        first_block = max(0, start // self.block_size - prefetch)
        last_block = min(self.n_blocks - 1, (end - 1) // self.block_size + prefetch)
        wanted = list(range(first_block, last_block + 1))

        with self._cv:
            with self._cache_lock:
                wanted = [b for b in wanted if b not in self._cache]
            for b in reversed(wanted):
                if b == self._inflight or b in self._queued_set:
                    continue
                self._work.appendleft(b)
                self._queued_set.add(b)
            while len(self._work) > MAX_QUEUED_REQUESTS:
                stale = self._work.pop()
                self._queued_set.discard(stale)
            self._cv.notify()

    def poll_updates(self) -> list:
        """Drain and return block indices that finished loading since the
        last call. Cheap, non-blocking -- safe to call from a UI tick."""
        out = []
        try:
            while True:
                out.append(self._result_queue.get_nowait())
        except Empty:
            pass
        return out

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # -- worker thread --------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not self._work and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                block = self._work.popleft()
                self._queued_set.discard(block)
                self._inflight = block

            start = block * self.block_size
            end = min(start + self.block_size, self.row_count)
            try:
                arr = read_rows(self._dataset, start, end, self.layout)
            except Exception:
                arr = np.full((max(end - start, 0), self.layout.n_columns), "<read error>", dtype=object)

            with self._cache_lock:
                self._cache[block] = arr
                self._cache.move_to_end(block)
                while len(self._cache) > self._max_cached_blocks:
                    self._cache.popitem(last=False)

            with self._cv:
                self._inflight = None

            self._result_queue.put(block)
