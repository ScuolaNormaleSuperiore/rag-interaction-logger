# Changelog

All notable changes to RAG Interaction Logger. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.0.2] - 2026-10-07

### Changed
- `queue_size` is documented as taking effect when the plugin is reactivated or the Cat
  restarts; the other settings apply from the next event.
- The README lists every way to run the tests and the command that builds the release ZIP.
- `run-tests.py` moved to `.tests/` and `package-plugin.py` to `.tools/`. Run them as
  `python .tests/run-tests.py` and `python .tools/package-plugin.py`.

### Fixed
- The Cat no longer imports `run-tests.py` and `package-plugin.py` when the plugin is
  installed from the repository: only the runtime modules are loaded.
- A purge with a large backlog now pauses one second between its cycles of batches instead
  of sending `DELETE` statements back to back.
- An error while the logger starts or stops its writer no longer makes the Cat fail the
  plugin activation or deactivation; it is reported as a safe warning instead.

## [0.0.1] - 2026-10-01

### Added
- First version: one row per question-and-reply turn in an external MySQL or MariaDB table,
  written asynchronously by a background worker, with Guardrails verdicts (optional),
  recalled documents and invoked tools.
