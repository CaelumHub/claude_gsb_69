#!/usr/bin/env bash
# Stop all nodes started by start_network.sh.
cd "$(dirname "$0")"
for pidfile in run/node*.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "stopped $pidfile ($pid)"
  fi
  rm -f "$pidfile"
done
echo "network stopped"
