#!/bin/sh
# Keep the bot running here: restart it whenever it exits, until this script
# is stopped. Output goes to bot.log (git-ignored).
#
# In a cloud session a background command is stopped after two hours and on
# a container restart; whoever runs this (Claude, in this repo) starts it
# again. A stale copy from before is stopped first, so there is never a
# second bot sending the same alerts.
cd "$(dirname "$0")" || exit 1
pkill -f "python3 -m kalshi_bot run" 2>/dev/null && sleep 2
while true; do
  python3 -m kalshi_bot run >> bot.log 2>&1
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) bot exited with $?, restarting in 10s" >> bot.log
  sleep 10
done
