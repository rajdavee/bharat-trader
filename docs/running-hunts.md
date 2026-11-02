# Running long hunts and backfills safely

The operations side of the edge hunt ([lab.md](lab.md), [the brief](research/edge-hunt.md),
[the results](research/edge-hunt-results.md)): how much data and time the
full archive takes, how much memory a hunt needs, and the lessons from the
night a hunt froze a laptop. Read it before you leave a backfill or a hunt
running unattended.

## What happened: the RAM incident (2 Nov 2026)

The first batch of edge-hunt runs went onto the full archive (2.1 GB of
bhavcopies from 2010) on a 16 GB Mac, while the 2003 to 2009 backfill was
also running. The option engines held every board of every results event in
memory, and the stock panels held every stock's whole history as Python
dicts at once. eh-09 passed 3 GB in its third trial, macOS swapped harder
and harder, and the machine froze: the hunt died at trial 4 of 6 and the
backfill died with it. Nothing was lost (both resume), but nothing tells
you a laptop is about to freeze until it does.

Lessons, all now in the code or in this page:

1. **The engines are memory-bounded** (#88). Option boards are packed and
   decoded through a bounded cache, results-day engines stream the F&O files
   event by event, stock panels load 40 stocks at a time. Results are
   identical (checked against digests from before the change). A hunt on
   the real archive now peaks at about 0.5 to 0.6 GB; the table per family
   is in [lab.md](lab.md#memory-what-a-hunt-needs-and-the-guard).
2. **Use the built-in guard.** `hunt run --max-mem-mb N` (default
   `lab_max_mem_mb` in `trader/config.json`, else 2048; 0 turns it off)
   pauses the run cleanly with exit code 4 when this process passes N MB:
   every finished trial is kept, the holdout stays sealed, and the same
   command carries on.
3. **Add an external watchdog for unattended runs.** The built-in guard sees
   only its own process, not the machine. A watchdog also kills the job
   when the machine as a whole runs low (another app, a second job).
4. **One heavy job at a time.** Never a hunt and a backfill together, never
   two hunts. Queue them in one script, one after the other.
5. **Interrupted is fine.** A killed hunt re-runs only the trial that was
   running (counted once); a killed backfill skips what it already stored.
   Trials logged by an older engine revision are refused on resume unless
   `--rerun-superseded` sets them aside, still charged to the trial count.

## The built-in guard

```bash
python3 trader-lab/lab.py hunt run eh-03-index-trend --dry-run           # hash, data coverage, trial count; runs nothing
python3 trader-lab/lab.py hunt run eh-03-index-trend --max-mem-mb 1500   # pause cleanly past 1.5 GB
python3 trader-lab/lab.py hunt run eh-09-event-runup --rerun-superseded  # older-engine trials set aside, still counted
python3 trader-lab/lab.py hunt status                                     # every verdict and the Benjamini-Hochberg line
```

It reads `resource.getrusage` (bytes on macOS, kilobytes on Linux).
Windows has no `resource` module, so there is no guard there: watch Task
Manager, or run hunts on a machine with more headroom.

## An external watchdog: memguard.sh

[`docs/examples/memguard.sh`](examples/memguard.sh) is the small macOS
watchdog used for the real runs. It starts the command at low priority
(`nice 10`), checks the resident memory of the command and its children
every 5 seconds, and kills the whole tree if it passes `LIMIT_MB` or if the
Mac's free memory (from `memory_pressure`) falls under `MIN_FREE_PCT`
(default 15%). It logs a line a minute, and the kill with its reason.

```sh
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
```

Set the watchdog's limit above the built-in guard's, so the clean pause
comes first and the kill is the backstop. Run several hunts in a queue, one
at a time:

```sh
mkdir -p hunt-logs
for h in eh-05-vrp-condor eh-09-event-runup; do
  docs/examples/memguard.sh 2500 "hunt-logs/$h.log" \
    python3 trader-lab/lab.py hunt run "$h" --max-mem-mb 2048
  echo "$h exit=$?" >> hunt-logs/progress.log
done
```

It is an example, not part of any skill: on Linux, replace
`memory_pressure` with the `MemAvailable` line of `/proc/meminfo`, or use
`systemd-run --user -p MemoryMax=2G` instead.

## How big and how long

Measured on the maintainer's machine, Nov 2026, at the default polite pause
(1.5 s between NSE calls). Your network and NSE's mood will differ.

| Job | Disk | Time |
| --- | --- | --- |
| `backfill --years 1` | – | about 30 minutes |
| `backfill --from 2010-01-01 --datasets all` | 2.1 GB | estimated 11.5 hours for the 5 daily datasets (4,384 sessions), more with the long-history ones; resumable |
| `backfill --from 2003-01-01 --to 2009-12-31 --datasets eq,fo,index,indexhist,settle,ca` (futures ideas need F&O from 2003) | about 0.4 GB more | about 7 hours |
| the whole archive, 2003 to 2026 | **about 2.5 GB** (F&O 2.0 GB, equity 334 MB, settle 52 MB, the rest under 20 MB each) | **about 20 hours** polite, spread over as many sittings as you like |
| `backfill --datasets lothist` (dated lot sizes) | 3 MB | about 20 minutes (370 calls, 58 MB downloaded) |
| one equity or futures hunt | a few MB of results | 15 seconds to 7 minutes |
| one option hunt | a few MB of results | 2 to 30 minutes (eh-09 took 30, eh-15 15) |

```bash
python3 trader-archive/archive.py backfill --from 2010-01-01 --datasets all
python3 trader-archive/archive.py backfill --from 2003-01-01 --to 2009-12-31 --datasets eq,fo,index,indexhist,settle,ca
python3 trader-archive/archive.py backfill --from 2010-01-01 --datasets lothist
python3 trader-archive/archive.py status
python3 trader-archive/archive.py verify
```

Do not lower `--pause` to go faster: NSE blocks impolite clients, and a
backfill that resumes tomorrow is cheaper than one that is blocked today.

## Lot sizes: the history the option hunts needed

NSE publishes only today's lot sizes, and the legacy F&O bhavcopies (to July
2024) have no lot column, so the first option runs had nothing to size a
2012 iron fly with. #90 built a dated lot history: every contract's lot on
every day, **exact** where NSE's own numbers exist (the UDiFF bhavcopy from
2024, NSE's market activity reports from March 2011, archived copies of
`fo_mktlots.csv` from 2008, the index lot circulars) and **reconstructed**
from the bhavcopy's notional turnover and open interest elsewhere. The
option engines refuse a reconstructed lot unless `allow_reconstructed_lots`
is true, and each scorecard reports the coverage (99.7% to 100% exact for
the windows eh-05, eh-09 and eh-15 used). The method is in
[archive.md](archive.md#fo-lot-sizes-over-time).

## Checklist before an unattended run

- Nothing else heavy is running (a backfill, another hunt, a big build).
- `hunt run ID --dry-run` passes: hash, data coverage, lots, trial count.
- The built-in guard is on (`--max-mem-mb`), and the watchdog's limit is
  above it.
- Logs go to a file you can read the next morning.
- The machine will not sleep (on macOS, `caffeinate -i` in front of the
  command).

Education and research tooling, not financial advice.
