"""The example config must always parse — it is the first thing a new user
copies, and a stale example is a broken front door."""
from pathlib import Path

from ddi_reconciler.config import load_config


def test_example_config_parses_and_is_lab_free():
    cfg = load_config(Path("config.example.toml"))
    assert cfg.spatium_base_url == "http://localhost:8000"
    zones = {e.zone for e in cfg.edges}
    assert zones == {"internal.example.com", "example.com"}
