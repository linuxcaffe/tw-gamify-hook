#!/usr/bin/env bash
# on-modify_zz-gamify.sh - scores a change: done, delete, due date pushed, undo. Named zz so it
# runs last: a hook that rejects the command runs first, and no points are paid for a change
# that never happened.
GAMIFY="${TW_TASK_DIR:-$HOME/.task}/scripts/gamify"
[ -x "$GAMIFY" ] && command -v python3 >/dev/null && exec "$GAMIFY" --hook modify "$@"
tail -n 1
