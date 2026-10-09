# Changelog

All notable changes to RAG Interaction Logger. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).


## [0.0.3] - 2026-10-09
### Fixed
- Fixed rresources management


## [0.0.2] - 2026-10-07
### Changed
- Test and packaging scripts moved to `.tests/` and `.tools/`, so the Cat no longer loads them.
- `queue_size` applies on reactivation; the README covers installation, tests and the changelog.
### Fixed
- The purge pauses one second between cycles of batches.
- An error at activation or deactivation no longer reaches the Cat.

## [0.0.1] - 2026-10-01
### Added
- First version: one row per turn in MySQL or MariaDB, written asynchronously, with optional Guardrails verdicts, recalled documents and tools.
