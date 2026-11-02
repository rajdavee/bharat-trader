#!/bin/bash
# memguard.sh LIMIT_MB LOGFILE cmd args...
# Runs cmd at low priority; kills it (and its children) if its memory passes
# LIMIT_MB or the Mac's free memory falls below MIN_FREE_PCT. Logs every minute.
LIMIT_MB=$1; LOG=$2; shift 2
MIN_FREE_PCT=${MIN_FREE_PCT:-15}
nice -n 10 "$@" >> "$LOG" 2>&1 &
PID=$!
n=0
tree_rss_mb() {  # resident memory of PID and its children, in MB
  local pids="$1 $(pgrep -P "$1" | tr '\n' ' ')"
  ps -o rss= -p $(echo $pids | tr ' ' ',') 2>/dev/null | awk '{s+=$1} END {printf "%d", s/1024}'
}
while kill -0 $PID 2>/dev/null; do
  rss=$(tree_rss_mb $PID)
  free=$(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/ {gsub("%","",$2); print $2}')
  free=${free:-100}
  if [ "${rss:-0}" -gt "$LIMIT_MB" ] || [ "$free" -lt "$MIN_FREE_PCT" ]; then
    echo "memguard: KILLED pid $PID at $(date '+%F %T') rss=${rss}MB free=${free}% (limit ${LIMIT_MB}MB, min free ${MIN_FREE_PCT}%)" >> "$LOG"
    pkill -TERM -P $PID; kill -TERM $PID; sleep 5; pkill -KILL -P $PID; kill -KILL $PID 2>/dev/null
    exit 137
  fi
  n=$((n+1)); [ $((n % 12)) -eq 0 ] && echo "memguard: $(date '+%T') rss=${rss}MB free=${free}%" >> "$LOG"
  sleep 5
done
wait $PID; exit $?
