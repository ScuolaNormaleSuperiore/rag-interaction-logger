"""Run the RAG Interaction Logger test suite."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SERVICE = "cheshire-cat-core"
PLUGIN_IN_CONTAINER = "/app/cat/plugins/rag-interaction-logger"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the logger test suite.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("-u", "--unit", action="store_true")
    scope.add_argument("-i", "--integration", action="store_true")
    parser.add_argument("-d", "--detailed", action="store_true")
    return parser.parse_args()


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
    return subprocess.run(pytest_command(detailed, "tests/unit"), cwd=REPO_ROOT).returncode


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


def run_container_tests(detailed: bool, integration_only: bool) -> int:
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
    command = [
        *compose,
        "exec",
        "-T",
        "-w",
        PLUGIN_IN_CONTAINER,
        SERVICE,
        "python",
        "-m",
        "pytest",
    ]
    if integration_only:
        command.append("tests/integration")
    if detailed:
        command.append("-v")
    environment = os.environ.copy()
    environment.setdefault("MSYS_NO_PATHCONV", "1")
    return subprocess.run(command, cwd=project_dir, env=environment, check=False).returncode


def main() -> int:
    args = parse_args()
    if args.unit:
        return run_unit_tests(args.detailed)
    return run_container_tests(args.detailed, args.integration)


if __name__ == "__main__":
    raise SystemExit(main())
