"""Guards for what Cheshire Cat imports from this folder, and for the test setup that keeps it small.

The Cat imports every `.py` under the plugin folder (`glob("**/*.py", recursive=True)` in
`cat/mad_hatter/plugin.py`, no exclusions) except what sits in a hidden folder, which
`glob` does not enter. A file the Cat loads that no plugin code needs is, at best, wasted
work and, at worst, what once broke a neighbouring plugin: a test that puts the plugin
folder on `sys.path` makes a bare `import settings` resolve to this plugin's `settings.py`.

A class check alone cannot tell needed files from useless ones: `record.py`, `schema.py`,
`db.py` and `writer.py` define no hook, tool, form, endpoint or override, and are needed all
the same. What can: start from the files that do define something the Cat registers and
follow the imports. Whatever is not reached is not part of the plugin.
"""

import ast
import configparser
import glob
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Loaded by the Cat although it does not need them, and harmless: nothing runs when they
# are imported (checked below). Listing a file here is a decision, not a default.
DEVELOPMENT_SCRIPTS = frozenset({"run-tests.py", "package-plugin.py"})

# What marks a module as something the Cat registers: the decorators of
# `cat.mad_hatter.decorators` and the `CatForm` base class.
CAT_DECORATORS = frozenset({"hook", "plugin", "tool", "form", "endpoint", "get", "post", "put", "delete"})


def files_the_cat_imports(root) -> list[str]:
    """The call the Cat makes: every `.py` under `root`, hidden folders skipped."""
    pattern = os.path.join(str(root), "**/*.py")
    return sorted(
        os.path.relpath(path, root).replace(os.sep, "/")
        for path in glob.glob(pattern, recursive=True)
    )


def _name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def defines_cat_objects(tree) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(_name(decorator) in CAT_DECORATORS for decorator in node.decorator_list):
                return True
            if isinstance(node, ast.ClassDef) and any(_name(base) == "CatForm" for base in node.bases):
                return True
    return False


def local_imports(tree, modules) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module.split(".")[0]]
            elif node.level:
                names = [alias.name for alias in node.names]
            else:
                names = []
        elif isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        else:
            continue
        found.update(name for name in names if name in modules)
    return found


def files_outside_the_plugin(root) -> list[str]:
    """The files the Cat imports that no plugin code, directly or through imports, needs."""
    files = files_the_cat_imports(root)
    stems = {Path(name).stem: name for name in files if "/" not in name}
    trees = {name: ast.parse((Path(root) / name).read_text(encoding="utf-8")) for name in files}
    reached = {name for name in files if defines_cat_objects(trees[name])}
    queue = list(reached)
    while queue:
        for stem in local_imports(trees[queue.pop()], set(stems)):
            if stems[stem] not in reached:
                reached.add(stems[stem])
                queue.append(stems[stem])
    return [name for name in files if name not in reached]


# ------------------------------------------------------------------ this repository


def test_the_cat_imports_nothing_that_the_plugin_does_not_need_except_the_known_scripts():
    extra = [name for name in files_outside_the_plugin(REPO_ROOT) if name not in DEVELOPMENT_SCRIPTS]

    assert extra == [], (
        f"the Cat would import {extra}, which no plugin code uses. Move test or tool files "
        "into a hidden folder (.tests/, .tools/), or, for an inert script, add it to "
        "DEVELOPMENT_SCRIPTS on purpose."
    )


def test_no_test_module_is_among_the_files_the_cat_imports():
    tests = [
        name for name in files_the_cat_imports(REPO_ROOT)
        if Path(name).name.startswith("test_") or Path(name).name == "conftest.py"
    ]

    assert tests == []
    assert (REPO_ROOT / ".tests").is_dir()


def test_the_runtime_files_the_cat_loads_are_exactly_the_python_files_of_the_zip():
    spec = importlib.util.spec_from_file_location("package_plugin", REPO_ROOT / "package-plugin.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    loaded = set(files_the_cat_imports(REPO_ROOT)) - DEVELOPMENT_SCRIPTS

    assert loaded == {name for name in package.INCLUDED_FILES if name.endswith(".py")}


def test_the_plugin_entry_points_are_found_by_their_decorators():
    entries = {
        name for name in files_the_cat_imports(REPO_ROOT)
        if defines_cat_objects(ast.parse((REPO_ROOT / name).read_text(encoding="utf-8")))
    }

    assert entries == {"rag_interaction_logger.py", "settings.py"}


@pytest.mark.parametrize("script", sorted(DEVELOPMENT_SCRIPTS))
def test_the_development_scripts_do_nothing_when_the_cat_imports_them(script):
    tree = ast.parse((REPO_ROOT / script).read_text(encoding="utf-8"))
    effects = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef,
                             ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the docstring
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            continue
        effects.append(ast.unparse(node)[:60])

    assert effects == []


# ------------------------------------------------------------------ the check itself


def build(tmp_path, files: dict):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_a_stray_file_and_a_visible_tests_folder_are_reported_and_a_hidden_one_is_not(tmp_path):
    build(tmp_path, {
        "plugin.py": "from .helper import x\n@hook('fast_reply')\ndef f(m, c): pass\n",
        "helper.py": "x = 1\n",
        "stray.py": "y = 2\n",
        "tests/test_visible.py": "z = 3\n",
        ".tests/test_hidden.py": "w = 4\n",
    })

    assert files_the_cat_imports(tmp_path) == ["helper.py", "plugin.py", "stray.py", "tests/test_visible.py"]
    assert files_outside_the_plugin(tmp_path) == ["stray.py", "tests/test_visible.py"]


def test_a_file_reached_only_through_another_helper_is_needed(tmp_path):
    build(tmp_path, {
        "plugin.py": "from record import a\n@plugin\ndef settings_model(): pass\n",
        "record.py": "from schema import b\na = b\n",
        "schema.py": "import deep\nb = deep.c\n",
        "deep.py": "c = 1\n",
    })

    assert files_outside_the_plugin(tmp_path) == []


def test_relative_imports_of_the_form_from_dot_import_are_followed(tmp_path):
    build(tmp_path, {
        "plugin.py": "from . import helper\n@tool(return_direct=True)\ndef t(x, cat): pass\n",
        "helper.py": "x = 1\n",
    })

    assert files_outside_the_plugin(tmp_path) == []


def test_a_form_class_and_an_endpoint_count_as_entry_points(tmp_path):
    build(tmp_path, {
        "form.py": "class Pizza(CatForm):\n    pass\n",
        "route.py": "@endpoint.get('/x')\ndef x(): pass\n",
        "orphan.py": "z = 1\n",
    })

    assert files_outside_the_plugin(tmp_path) == ["orphan.py"]


def test_a_plugin_with_no_registered_object_leaves_every_file_unreached(tmp_path):
    build(tmp_path, {"a.py": "import b\n", "b.py": "x = 1\n"})

    assert files_outside_the_plugin(tmp_path) == ["a.py", "b.py"]


# ------------------------------------------------------------------ the test setup that keeps it so


def pytest_section():
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini", encoding="utf-8")
    return parser["pytest"]


def test_pytest_is_pointed_at_the_hidden_folder_and_may_enter_it_from_a_directory():
    section = pytest_section()

    assert section["testpaths"].strip() == ".tests"
    # pytest skips directories matching `.*` by default, so `pytest .` would collect nothing.
    assert ".*" not in section["norecursedirs"].split()


def test_pytest_given_a_directory_still_collects_the_hidden_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider", "."],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )

    output = result.stdout.replace("\\", "/")
    assert result.returncode == 0, result.stdout[-400:] + result.stderr[-400:]
    assert ".tests/unit/test_record.py" in output
    assert ".tests/unit/test_repository_layout.py" in output


def test_the_pre_commit_hook_fails_when_the_test_folder_is_missing(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    # The test is about the logic, not the line endings: bash on Linux rejects the CRLF
    # that the script carries in a Windows checkout, so the copy is normalised first.
    script = (REPO_ROOT / ".githooks" / "run-unit-tests.sh").read_bytes().replace(b"\r\n", b"\n")
    (hooks / "run-unit-tests.sh").write_bytes(script)

    result = subprocess.run(
        [bash, str(hooks / "run-unit-tests.sh")], capture_output=True, text=True, timeout=60
    )

    assert result.returncode != 0
    assert ".tests/unit" in result.stderr and "blocked" in result.stderr.lower()
