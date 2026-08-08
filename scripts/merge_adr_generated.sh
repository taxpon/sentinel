#!/usr/bin/env bash
# Git merge driver for the two files `scripts/gen_adr_index.py` generates.
#
# Registered by `make setup-git` and selected by `.gitattributes`. Git calls it with the path it is
# merging and the temporary file it wants the result in:
#
#     merge_adr_generated.sh %P %A
#
# Neither side of the conflict is worth reading. By the time git reaches these files it has already
# written every non-conflicting change to the worktree — including the added records themselves,
# which are new files and so never conflict — so regenerating from `docs/adr/` produces exactly the
# index the merged tree should have.
#
# Exiting non-zero leaves the conflict for a human, which is the right answer whenever the generator
# cannot speak for the result: a record with malformed front matter, or a merge that is not being
# run from the repository root.
set -euo pipefail

path="$1"
result="$2"

command -v uv >/dev/null 2>&1 || exit 1
[ -d docs/adr ] || exit 1

uv run --quiet scripts/gen_adr_index.py >/dev/null 2>&1 || exit 1

# The generator writes the worktree copy; git wants it in its own temporary file.
cp "$path" "$result"
