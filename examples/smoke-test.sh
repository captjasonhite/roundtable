#!/usr/bin/env bash
# One real bench run through the queue, end to end, on the GPU.
#
# Two fast models, thinking only, judging on, short token caps -- enough to prove
# the worker drives the real bench script, that judges emit the RANKING: line,
# and that the live report fills in as runs land. Expect roughly 5-9 minutes:
# four model loads (2 runs + 2 judges), one at a time.
#
# The report path is printed as soon as the session directory appears; open it
# and leave it open -- the page reloads itself until the run finishes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RT="$HERE/../bin/roundtable"
JOB="${1:-$HERE/smoke-job.json}"

echo "queueing $(basename "$JOB")"
"$RT" submit "$JOB"

# Report the session directory the moment the worker creates it.
(
  for _ in $(seq 1 600); do
    session="$("$RT" status | sed -n 's|.*\(/.*creative-bench/[0-9-]*\).*|\1|p')"
    if [[ -n "$session" ]]; then
      echo
      echo "  live report: $session/report.html"
      echo "  (open it now; it reloads itself until the run is done)"
      echo
      break
    fi
    sleep 2
  done
) &

"$RT" work --drain
wait 2>/dev/null || true

echo
"$RT" status
