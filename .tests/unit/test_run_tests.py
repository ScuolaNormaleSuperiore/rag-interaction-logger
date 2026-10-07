"""The runner's local environment file: it must feed the database tests and nothing else."""

import importlib.util
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_runner():
    spec = importlib.util.spec_from_file_location("run_tests", REPO_ROOT / ".tests" / "run-tests.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write(tmp_path, text):
    path = tmp_path / ".ril-test.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_file_gives_nothing(tmp_path):
    assert load_runner().read_local_environment(tmp_path / "absent.env") == {}


def test_names_and_values_are_read_with_comments_blank_lines_and_quotes(tmp_path):
    path = write(
        tmp_path,
        "# a comment\n\nRIL_TEST_DB_HOST = db.example.org\n"
        "RIL_TEST_DB_PORT=3307\nRIL_TEST_DB_USER='ril admin'\n"
        'RIL_TEST_DB_PASSWORD="p=s w"\nnot a setting line\n',
    )

    assert load_runner().read_local_environment(path) == {
        "RIL_TEST_DB_HOST": "db.example.org",
        "RIL_TEST_DB_PORT": "3307",
        "RIL_TEST_DB_USER": "ril admin",
        "RIL_TEST_DB_PASSWORD": "p=s w",
    }


def test_a_value_may_contain_an_equals_sign(tmp_path):
    path = write(tmp_path, "RIL_TEST_DB_PASSWORD=abc=def==\n")

    assert load_runner().read_local_environment(path) == {"RIL_TEST_DB_PASSWORD": "abc=def=="}


def test_blank_values_count_as_not_set(tmp_path):
    path = write(tmp_path, "RIL_TEST_DB_TLS=\nRIL_TEST_DB_USER=''\nRIL_TEST_DB_HOST=h\n")

    assert load_runner().read_local_environment(path) == {"RIL_TEST_DB_HOST": "h"}


def test_only_the_database_test_names_are_accepted(tmp_path):
    path = write(tmp_path, "PATH=/evil\nPYTHONPATH=/evil\nRIL_TEST_DB_HOST=h\nANYTHING=1\n")

    assert load_runner().read_local_environment(path) == {"RIL_TEST_DB_HOST": "h"}


def test_a_variable_already_in_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"RIL_TEST_DB_HOST": "from-the-environment"})
    path = write(tmp_path, "RIL_TEST_DB_HOST=from-the-file\nRIL_TEST_DB_USER=from-the-file\n")

    applied = load_runner().apply_local_environment(path)

    assert os.environ["RIL_TEST_DB_HOST"] == "from-the-environment"
    assert os.environ["RIL_TEST_DB_USER"] == "from-the-file"
    assert applied == ["RIL_TEST_DB_USER"]


def test_a_server_is_available_only_with_a_host_and_a_user(monkeypatch):
    runner = load_runner()
    monkeypatch.setattr(os, "environ", {})
    assert runner.database_settings_available() is False

    os.environ["RIL_TEST_DB_HOST"] = "h"
    assert runner.database_settings_available() is False

    os.environ["RIL_TEST_DB_USER"] = "u"
    assert runner.database_settings_available() is True


def test_the_example_file_lists_every_name_the_runner_accepts_and_holds_no_secret():
    runner = load_runner()
    example = (REPO_ROOT / ".ril-test.env.example").read_text(encoding="utf-8")

    for name in runner.DATABASE_ENVIRONMENT:
        assert f"{name}=" in example
    values = {line.partition("=")[0]: line.partition("=")[2] for line in example.splitlines()
              if line and not line.startswith("#") and "=" in line}
    assert values["RIL_TEST_DB_USER"] == "" and values["RIL_TEST_DB_PASSWORD"] == ""
