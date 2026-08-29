from __future__ import annotations

from copy import deepcopy

class ContextCache:
    def __init__(self) -> None:
        self._items: dict[str, object] = {}

    def get(self, key: str):
        item = self._items.get(key)
        if item is None:
            return None
        return deepcopy(item)

    def set(self, key: str, value) -> None:
        self._items[key] = deepcopy(value)
