"""Run the RAG Interaction Logger test suite."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE = "cheshire-cat-core"
PLUGIN_IN_CONTAINER = "/app/cat/plugins/rag-interaction-logger"
PROJECT_COPY_IN_CONTAINER = "/tmp/ril-PROJECT.md"
LOCAL_ENVIRONMENT_FILE = REPO_ROOT / ".ril-test.env"
DATABASE_ENVIRONMENT = (
    "RIL_TEST_DB_HOST",
    "RIL_TEST_DB_PORT",
    "RIL_TEST_DB_USER",
    "RIL_TEST_DB_PASSWORD",
    "RIL_TEST_DB_REQUIRE_SSL",
    "RIL_TEST_DB_TLS",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the logger test suite.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("-u", "--unit", action="store_true")
    scope.add_argument("-i", "--integration", action="store_true")
    scope.add_argument(
        "-b",
        "--database",
        action="store_true",
        help="tests against a real MySQL or MariaDB server (see .ril-test.env.example)",
    )
    parser.add_argument("-d", "--detailed", action="store_true")
    return parser.parse_args()


def read_local_environment(path: Path = LOCAL_ENVIRONMENT_FILE) -> dict[str, str]:
    """Read `NAME=value` lines for the database tests from a git-ignored local file.

    Only the names in `DATABASE_ENVIRONMENT` are accepted, so the file cannot set
    anything else in the environment; blank values count as not set.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in DATABASE_ENVIRONMENT and value:
            values[name] = value
    return values


def apply_local_environment(path: Path = LOCAL_ENVIRONMENT_FILE) -> list[str]:
    """Add the file's values to the environment; a variable already set wins."""
    applied = []
    for name, value in read_local_environment(path).items():
        if not os.environ.get(name):
            os.environ[name] = value
            applied.append(name)
    return applied


def database_settings_available() -> bool:
    return all(os.environ.get(name) for name in ("RIL_TEST_DB_HOST", "RIL_TEST_DB_USER"))


def pytest_command(detailed: bool, path: str | None = None) -> list[str]:
    command = [sys.executable, "-m", "pytest"]
    if path:
        command.append(path)
    if detailed:
        command.append("-v")
    return command


def run_unit_tests(detailed: bool) -> int:
    probe = subprocess.run(
        [sys.executable, "-c", "import pytest"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode:
        print(f"pytest is not installed: {sys.executable}", file=sys.stderr)
        return 1
    return subprocess.run(pytest_command(detailed, ".tests/unit"), cwd=REPO_ROOT).returncode


def compose_dir() -> Path:
    directory = (REPO_ROOT / "../../../..").resolve()
    if not (directory / "compose.yml").is_file():
        raise FileNotFoundError(f"compose.yml not found in {directory}")
    return directory


def compose_command() -> list[str] | None:
    if docker := shutil.which("docker"):
        if subprocess.run([docker, "compose", "version"], capture_output=True).returncode == 0:
            return [docker, "compose"]
    if docker_compose := shutil.which("docker-compose"):
        return [docker_compose]
    return None


def run_container_tests(detailed: bool, integration_only: bool, database_only: bool = False) -> int:
    try:
        project_dir = compose_dir()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    compose = compose_command()
    if compose is None:
        print("Docker Compose is unavailable; run --unit instead.", file=sys.stderr)
        return 1
    running = subprocess.run(
        [*compose, "ps", "-q", SERVICE],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if not running.stdout.strip():
        print(f"The {SERVICE} container is not running.", file=sys.stderr)
        return 1

    # The full run includes the database tests whenever a server is described, in the
    # environment or in `.ril-test.env`; `--integration` never needs one.
    with_database = database_only or (not integration_only and database_settings_available())
    if database_only and not database_settings_available():
        print(
            "Describe the database server to run the database tests: copy "
            ".ril-test.env.example to .ril-test.env and fill it in, or set "
            "RIL_TEST_DB_HOST, RIL_TEST_DB_USER and RIL_TEST_DB_PASSWORD.",
            file=sys.stderr,
        )
        return 1
    if not integration_only and not with_database:
        print(
            "Database tests will be SKIPPED: no server is described (.ril-test.env or "
            "RIL_TEST_DB_* variables).",
            file=sys.stderr,
        )
    forwarded: list[str] = []
    if with_database:
        for name in DATABASE_ENVIRONMENT:
            if os.environ.get(name):
                forwarded += ["-e", name]
        # DEV/ is a link to a Windows folder and is not visible inside the container.
        project = REPO_ROOT / "DEV" / "AGENTS" / "PROJECT.md"
        if project.is_file():
            copied = subprocess.run(
                [*compose, "cp", str(project), f"{SERVICE}:{PROJECT_COPY_IN_CONTAINER}"],
                cwd=project_dir,
                capture_output=True,
                check=False,
            )
            if copied.returncode == 0:
                forwarded += ["-e", f"RIL_TEST_PROJECT_MD={PROJECT_COPY_IN_CONTAINER}"]
    command = [
        *compose,
        "exec",
        "-T",
        *forwarded,
        "-w",
        PLUGIN_IN_CONTAINER,
        SERVICE,
        "python",
        "-m",
        "pytest",
    ]
    if database_only:
        command.append(".tests/database")
    elif integration_only:
        command.append(".tests/integration")
    if detailed:
        command.append("-v")
    environment = os.environ.copy()
    environment.setdefault("MSYS_NO_PATHCONV", "1")
    return subprocess.run(command, cwd=project_dir, env=environment, check=False).returncode


def main() -> int:
    args = parse_args()
    if args.unit:
        return run_unit_tests(args.detailed)
    apply_local_environment()
    return run_container_tests(args.detailed, args.integration, args.database)


if __name__ == "__main__":
    raise SystemExit(main())
