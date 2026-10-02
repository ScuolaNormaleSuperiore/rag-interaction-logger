"""The Cat imports plugin files as `cat.plugins.<folder>.<module>`, not top-level."""

import importlib
from pathlib import Path
import sys
import types

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "cat_plugin_probe"


@pytest.fixture
def plugin_package():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(REPO_ROOT)]
    sys.modules[PACKAGE] = package
    yield package
    for name in [name for name in sys.modules if name == PACKAGE or name.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]


@pytest.mark.parametrize("module", ["record", "schema", "db", "writer"])
def test_runtime_modules_load_as_submodules_of_the_plugin_package(plugin_package, module):
    imported = importlib.import_module(f"{PACKAGE}.{module}")

    assert imported.__name__ == f"{PACKAGE}.{module}"


def test_sibling_imports_resolve_inside_the_package_not_from_sys_path(plugin_package):
    importlib.import_module(f"{PACKAGE}.db")

    assert f"{PACKAGE}.record" in sys.modules
    assert f"{PACKAGE}.schema" in sys.modules
