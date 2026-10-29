"""A memo the server's request threads can share.

FastAPI runs the stats endpoints on a thread pool, and the warm-up thread fills
the same caches at start. A bare dict there races: two misses evict the same
oldest key, a prune iterates while another request inserts, a touch pops a key
another request is reading. Each of those surfaced as a 500. Every step here
holds one lock; the work of computing a value does not.

Computing it is guarded per key instead (`making`): a page asks for one player
in several requests at once, and the phone switches tabs faster than a cold
player derives, so each request deriving the same answer on its own cost a
multiple of one. On the phone's VM four of them took 436 s.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class LRU(Generic[K, V]):
    """A map that forgets its least recently used entry past `cap` (None: never)."""

    def __init__(self, cap: int | None = None) -> None:
        self.cap = cap
        self._d: OrderedDict[K, V] = OrderedDict()
        self._lock = threading.Lock()
        #: Per key being computed: its lock, and how many threads hold or wait on it.
        self._making: dict[K, list] = {}

    def get(self, key: K, default: V | None = None) -> V | None:
        """The value, marked as just used; `default` when absent."""
        with self._lock:
            if key not in self._d:
                return default
            self._d.move_to_end(key)
            return self._d[key]

    def __setitem__(self, key: K, value: V) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            if self.cap is not None:
                while len(self._d) > self.cap:
                    self._d.popitem(last=False)

    @contextmanager
    def making(self, key: K) -> Iterator[None]:
        """Held while one thread computes `key`'s value: another that missed the
        same key waits here, then looks again and finds it. Never hold it across a
        wait for something else -- the warm-up's pause until requests are done is
        one, since a request waiting here keeps it from ever being done."""
        with self._lock:
            entry = self._making.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if not entry[1]:
                    del self._making[key]

    def prune(self, drop: Callable[[K], bool]) -> None:
        """Forget every key `drop` says to."""
        with self._lock:
            for key in [k for k in self._d if drop(k)]:
                del self._d[key]

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)

    def __iter__(self) -> Iterator[K]:
        """The keys at one moment, oldest first."""
        with self._lock:
            return iter(list(self._d))

    def __contains__(self, key: object) -> bool:
        return key in self._d
