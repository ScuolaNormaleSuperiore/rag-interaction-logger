#!/usr/bin/env bash
set -euo pipefail

echo "[RIL pre-commit] Running secret scan..." >&2

if ! command -v git >/dev/null 2>&1; then
	exit 0
fi

if git rev-parse --verify HEAD >/dev/null 2>&1; then
	diff_output="$(git diff --cached --unified=0 --no-color --diff-filter=ACMRTUXB)"
else
	# Initial commit: compare against empty tree.
	empty_tree="$(git hash-object -t tree /dev/null)"
	diff_output="$(git diff --cached --unified=0 --no-color --diff-filter=ACMRTUXB "$empty_tree")"
fi

if [ -z "$diff_output" ]; then
	exit 0
fi

# No path is excluded from the scan. Generated and cache files should be kept
# out of the index through `.gitignore`, never by weakening this guard.
added_lines="$(printf '%s\n' "$diff_output" | awk '
	/^\+\+\+ / { next }
	/^\+/ {
		sub(/^\+/, "", $0)
		print
	}
')"

if [ -z "$added_lines" ]; then
	exit 0
fi

# There is deliberately **no exemption mechanism**, per line or per path.
#
# One existed briefly on 2026-08-06, a `pragma: allowlist secret` marker, and it
# was removed the same day with the tests that were its only reason to exist. The
# reasoning is worth keeping, because the next person who needs to commit a line
# that looks like a secret will reach for it again: an escape hatch in a secret
# scan is a way to silence a real detection, and this one had no test left to
# prove it stayed narrow.
#
# If a legitimate need does come back — a fixture, an example in documentation —
# reintroduce it per line rather than per path, so the exemption sits next to the
# value it covers and is visible in the diff a reviewer reads. Use
# `detect-secrets`' own spelling, so the markers survive if that tool is ever
# adopted, and add the tests that prove the exemption applies to one line only.

# High-signal secret patterns only (to reduce false positives).
#
# The last one is quoted with $'...' and not '...', and that single character is
# load-bearing: `\x27` is an ANSI-C escape that bash expands **only** inside
# $'...'. Written in ordinary single quotes it reached grep literally, so the
# character class meant {" \ x 2 7} instead of {" '} — which silently did two
# things. It never matched a single-quoted value, the entire reason `\x27` was
# there; and it stopped matching any value whose characters included x, 2, 7 or a
# backslash, so a key made of digits slipped through while one made of letters
# alone was caught. Found on 2026-08-06 by tests that executed this script in disposable
# repositories; they were removed on 2026-09-08 with their Git dependency, so
# nothing catches the next one.
#
# No verbatim example is written here on purpose: this file is scanned by the very
# patterns it defines, and a sample assignment in a comment would block every
# commit that touches it.
patterns=(
	'-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----'
	'(AKIA|ASIA)[A-Z0-9]{16}'
	'gh[pousr]_[A-Za-z0-9]{36,255}'
	'github_pat_[A-Za-z0-9_]{20,}'
	# Hugging Face user access token. It is not currently a logger setting, but the
	# generic scan keeps it protected if it appears in a fixture or support note.
	'hf_[A-Za-z0-9]{20,}'
	'xox[baprs]-[A-Za-z0-9-]{10,}'
	'sk-[A-Za-z0-9]{20,}'
	'[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]]+:[^[:space:]]+@[^[:space:]]+'
	$'(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password|passwd|pwd|secret)[[:space:]]*[:=][[:space:]]*["\x27][^"\x27]{8,}["\x27]'
)

found=0
for pattern in "${patterns[@]}"; do
	# `-e` is not optional here. The private-key pattern starts with `-----`, so
	# without it grep reads the pattern as options and dies with
	# `unknown option -- ---BEGIN ...`. The `2>/dev/null` then hid the message and
	# `|| true` swallowed the exit code, so the most serious pattern in this list
	# reported nothing and every commit carrying a private key passed the gate.
	# Found on 2026-08-06 by tests that executed this script in disposable
# repositories; they were removed on 2026-09-08 with their Git dependency, so
# nothing catches the next one.
	# Exit 1 means "no match". Any other non-zero status means the scanner itself
	# failed (for example because a newly added pattern is invalid), and must block
	# the commit instead of silently degrading to "no secret found".
	if matches="$(grep -E -i -n -e "$pattern" <<< "$added_lines" 2>&1)"; then
		:
	else
		grep_status=$?
		if [ "$grep_status" -ne 1 ]; then
			echo "[RIL pre-commit] Secret scan failed while evaluating a pattern:" >&2
			printf '%s\n' "$matches" >&2
			echo "Commit blocked because the secret scan did not complete." >&2
			exit 2
		fi
		matches=""
	fi
	if [ -n "$matches" ]; then
		if [ "$found" -eq 0 ]; then
			echo "[RIL pre-commit] Potential secret detected in staged changes:" >&2
			found=1
		fi
		printf '%s\n' "$matches" >&2
	fi

done

if [ "$found" -eq 1 ]; then
	echo >&2
	echo "Commit blocked. Remove secrets or move safe examples outside staged changes." >&2
	echo "If this is a false positive, adjust the pattern list in .githooks/check-staged-secrets.sh." >&2
	exit 1
fi

exit 0
