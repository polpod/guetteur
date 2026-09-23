from __future__ import annotations

import pytest

from guetteur.store import Store


@pytest.fixture
def store() -> Store:
    return Store(":memory:")
