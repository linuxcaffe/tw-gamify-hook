#!/usr/bin/env bash
# on-launch_gamify.sh - fires on every task command, so it starts no Python unless it must.
# Delivers the last sweep's verdict once, and starts a detached sweep at most every 30 minutes.
TASK_DIR="${TW_TASK_DIR:-$HOME/.task}"
GAMIFY="$TASK_DIR/scripts/gamify"
STAMP="$TASK_DIR/logs/gamify.stamp"
RECAP="$TASK_DIR/logs/gamify-recap.txt"

[ -x "$GAMIFY" ] || exit 0
[ -s "$RECAP" ] && "$GAMIFY" --recap
[ -n "$(find "$STAMP" -mmin -30 2>/dev/null)" ] && exit 0
mkdir -p "$TASK_DIR/logs"
touch "$STAMP"
nohup "$GAMIFY" --refresh </dev/null >/dev/null 2>&1 &
exit 0
