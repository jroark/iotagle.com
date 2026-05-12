"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    """Read a fixture file as text. Use: ``fixture_text("ddg_results.html")``."""

    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def fixture_bytes():
    """Read a fixture file as bytes."""

    def _read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _read
