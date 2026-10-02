"""Tests for the explicit runtime release package."""

import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_package_module():
    path = REPO_ROOT / "package-plugin.py"
    spec = importlib.util.spec_from_file_location("package_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_every_runtime_module_is_in_the_explicit_package():
    package = load_package_module()
    runtime_modules = {
        path.name
        for path in REPO_ROOT.glob("*.py")
        if path.name not in {"package-plugin.py", "run-tests.py"}
    }

    assert runtime_modules <= set(package.INCLUDED_FILES)
    assert package.validate_included_files()


def test_package_excludes_development_material():
    package = load_package_module()

    assert not [
        name
        for name in package.INCLUDED_FILES
        if name.startswith(("tests/", "DEV/", "DOC/", ".githooks/"))
    ]


def test_the_zip_holds_only_the_runtime_files_under_the_plugin_folder(tmp_path):
    package = load_package_module()
    metadata = package.load_metadata()
    slug = package.plugin_slug(metadata)
    zip_path = tmp_path / "plugin.zip"

    package.build_zip(zip_path, slug, package.validate_included_files())

    with ZipFile(zip_path) as archive:
        names = archive.namelist()
        shipped = json.loads(archive.read(f"{slug}/plugin.json"))
    assert names and all(name.startswith(f"{slug}/") for name in names)
    assert sorted(name.split("/", 1)[1] for name in names) == sorted(package.INCLUDED_FILES)
    for private in ("tests/", "DEV/", "settings.json", "__pycache__", ".githooks", "dist/", "AGENTS", "CLAUDE"):
        assert not [name for name in names if private in name], private
    assert shipped["version"] == metadata["version"]


def test_every_requirement_line_is_a_valid_unpinned_requirement():
    requirement = pytest.importorskip("packaging.requirements").Requirement
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert lines and all(line.strip() and not line.startswith("#") for line in lines)
    for line in lines:
        parsed = requirement(line)
        assert "==" not in str(parsed.specifier)
