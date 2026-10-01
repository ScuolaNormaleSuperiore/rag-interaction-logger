"""Tests for the explicit runtime release package."""

import importlib.util
from pathlib import Path


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
