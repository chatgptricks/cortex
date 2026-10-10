"""Tests use reserved example identities, never the deployment's private roster."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def example_private_roster(monkeypatch):
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(Path(__file__).parent / "fixtures/roster.example.json"))
