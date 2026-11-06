#!/usr/bin/env bash
# on-add_zz-gamify.sh - scores a new task. Named zz so it runs last: a hook that rejects the
# command runs first, and no points are paid for a change that never happened.
GAMIFY="${TW_TASK_DIR:-$HOME/.task}/scripts/gamify"
[ -x "$GAMIFY" ] && command -v python3 >/dev/null && exec "$GAMIFY" --hook add "$@"
tail -n 1
