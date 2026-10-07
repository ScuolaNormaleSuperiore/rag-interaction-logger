"""Build the explicit distributable ZIP for RAG Interaction Logger."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
INCLUDED_FILES = (
    "plugin.json",
    "README.md",
    "LICENSE",
    "requirements.txt",
    "db.py",
    "record.py",
    "schema.py",
    "settings.py",
    "writer.py",
    "rag_interaction_logger.py",
)


def load_metadata() -> dict:
    with (REPO_ROOT / "plugin.json").open(encoding="utf-8") as metadata_file:
        return json.load(metadata_file)


def plugin_slug(metadata: dict) -> str:
    return metadata["plugin_url"].rstrip("/").rsplit("/", 1)[-1]


def validate_included_files() -> list[Path]:
    missing = [name for name in INCLUDED_FILES if not (REPO_ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(", ".join(f"missing package file: {name}" for name in missing))
    return [REPO_ROOT / name for name in INCLUDED_FILES]


def build_zip(zip_path: Path, slug: str, files: list[Path]) -> None:
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source, (Path(slug) / source.name).as_posix())


def main() -> int:
    try:
        metadata = load_metadata()
        DIST_DIR.mkdir(exist_ok=True)
        zip_path = DIST_DIR / f"{plugin_slug(metadata)}-{metadata['version']}.zip"
        build_zip(zip_path, plugin_slug(metadata), validate_included_files())
    except Exception as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1
    print(f"Created: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
