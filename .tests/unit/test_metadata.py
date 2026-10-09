"""Tests for immutable plugin identity and shipped defaults."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_plugin_metadata_matches_the_logger_identity():
    metadata = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))

    assert metadata["name"] == "RAG Interaction Logger"
    assert metadata["version"] == "0.0.3"
    assert metadata["min_cat_version"] == metadata["max_cat_version"] == "1.9.2"


def test_requirements_are_the_specified_dependencies():
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert lines == [
        "PyMySQL>=1.1",
    ]
