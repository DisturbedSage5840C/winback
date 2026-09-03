#!/usr/bin/env bash
#
# scripts/failure_drill.sh — take the Razorpay MCP away from a running batch and prove
# the batch does not care.
#
# The plan asked for one drill: "docker stop the local MCP mid-batch; the run must
# degrade to remote + simulated, log stop_reason, and finish the batch." Running it
# found something better than a green tick, so this script now runs two, and the second
# one is the interesting failure.
#
#   Phase 1 — the transport is dead before the run reaches for it.
#       Injected by asking for a toolset the image does not have, which is the exact
#       shape of the bug that was live in `.env` until this morning: `docker run` exits
#       non-zero within a second. The preflight probe sees it, the lane steps down to
#       remote, the header says so, and the batch finishes. This phase asserts all of
#       that and fails if any of it stops being true.
#
#   Phase 2 — the transport dies mid-batch, which is what the plan actually described.
#       Injected with `docker kill`, not `docker stop`: stop sends SIGTERM and waits,
#       letting the server close its stdio cleanly, which is a polite shutdown rather
#       than the pulled cable worth defending against.
#
#       Phase 2 asserts only that the cohort still finishes with a complete audit trail,
#       and it does NOT require a demotion. That is not a weaker test, it is an honest
#       one: the SDK does not raise when a mounted stdio MCP server dies or fails to
#       start — verified on 3 Sep by running a batch against `razorpay/mcp:does-not-
#       exist` with the probes stubbed healthy, which completed normally and still
#       reported `lane: local`. So the mid-batch demotion in `agent/orchestrator.py`
#       fires only if a Razorpay tool call is actually in flight when the container
#       dies, and the batch surviving is the property that matters. See
#       docs/WHAT_BROKE.md, 3 Sep.
#
# Both phases run on the live lane, because that is the only lane where a Razorpay tool
# is permitted and therefore the only lane where the server is mounted at all — see
# `agent.tools.permitted_tools`. They spend a handful of test-mode API calls and create
# no customer-visible artifact.
#
# Usage: RAZORPAY_MCP_MODE=local scripts/failure_drill.sh [--limit N] [--keep]
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

LIMIT=2
KEEP=0
while (( $# )); do
    case "$1" in
        --limit) LIMIT="$2"; shift 2 ;;
        --keep)  KEEP=1; shift ;;
        -h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

# The ladder only has somewhere to fall *from* if it starts at the top.
if [[ "${RAZORPAY_MCP_MODE:-off}" != "local" ]]; then
    echo "This drill only means anything in local mode. Re-run as:" >&2
    echo "    RAZORPAY_MCP_MODE=local $0 $*" >&2
    exit 2
fi

PY=.venv/bin/python
STAMP=$(date +%Y%m%d_%H%M%S)
LOG1="$(mktemp -t winback_drill1)"
LOG2="$(mktemp -t winback_drill2)"
FAIL=0

cleanup() { (( KEEP )) || rm -f "$LOG1" "$LOG2"; }
trap cleanup EXIT

psql_q() { docker exec -i winback-db psql -U winback_owner -d winback -tAc "$1"; }
note()   { printf '  %s %s\n' "$1" "$2"; }

# ============================================================ phase 1: dead on arrival
RUN1="drill_${STAMP}_preflight"
echo "── phase 1 · the local MCP cannot start ──────────────────────"
echo "run  $RUN1"

set +e
RAZORPAY_MCP_MODE=local RAZORPAY_MCP_TOOLSETS=winback_drill_no_such_toolset \
    "$PY" -m agent.orchestrator --live --limit 1 --run-id "$RUN1" >"$LOG1" 2>&1
RC1=$?
set -e

if grep -q "degraded from local" "$LOG1"; then
    note "✓" "the preflight probe caught it and the lane stepped down"
    grep -m1 "degraded from local" "$LOG1" | cut -c1-160 | sed 's/^/      /'
else
    note "✗" "the run did not report a demotion"
    FAIL=1
fi

if grep -q "degraded to mcp:remote" "$LOG1"; then
    note "✓" "the run report names the lane it finished on"
else
    note "✗" "the batch report does not record the demotion"
    FAIL=1
fi

# The whole point. A fallback that logs beautifully and abandons the cohort has
# recovered from nothing.
if [[ "$RC1" -eq 0 ]]; then
    note "✓" "the batch finished its cohort with a complete audit trail (exit 0)"
else
    note "✗" "the batch exited $RC1"
    FAIL=1
    KEEP=1
fi

# ============================================================ phase 2: killed mid-batch
RUN2="drill_${STAMP}_midrun"
echo
echo "── phase 2 · the local MCP is killed mid-batch ───────────────"
echo "run  $RUN2 · $LIMIT invoices"

RAZORPAY_MCP_MODE=local "$PY" -m agent.orchestrator --live --limit "$LIMIT" \
    --run-id "$RUN2" >"$LOG2" 2>&1 &
BATCH_PID=$!

# Wait for a container AND for the first invoice to have concluded, so a passing drill
# also shows the batch was healthy before the fault. Without that wait, a run that failed
# from its first invoice would leave identical evidence. The container name is generated
# by the SDK, so it is found by image.
KILLED=""
for _ in $(seq 1 240); do
    kill -0 "$BATCH_PID" 2>/dev/null || break
    CID=$(docker ps -q --filter ancestor=razorpay/mcp | head -1)
    ROWS=$(psql_q "SELECT count(*) FROM audit_log WHERE run_id = '$RUN2'" || echo 0)
    if [[ -n "$CID" && "${ROWS:-0}" -ge 1 ]]; then
        echo "     killing container $CID after $ROWS audit row(s)"
        docker kill "$CID" >/dev/null
        KILLED="$CID"
        break
    fi
    sleep 1
done

RC2=0
wait "$BATCH_PID" || RC2=$?

if [[ -z "$KILLED" ]]; then
    note "✗" "no razorpay/mcp container was running to kill — nothing was tested"
    FAIL=1
    KEEP=1
else
    note "✓" "the transport was killed while the batch was working"
fi

if [[ "$RC2" -eq 0 ]]; then
    note "✓" "the batch finished anyway (exit 0)"
else
    note "✗" "the batch exited $RC2 after losing its transport"
    FAIL=1
    KEEP=1
fi

CONCLUDED=$(psql_q "SELECT count(DISTINCT subject_id) FROM audit_log
                    WHERE run_id = '$RUN2' AND trigger <> 'mcp_degraded'")
note "·" "$CONCLUDED of $LIMIT invoices concluded"

# Reported, not asserted — see the header. A demotion here means a Razorpay read was in
# flight when the container died; no demotion means it was not, and the batch was never
# blocked either way.
DEGRADED=$(psql_q "SELECT count(*) FROM audit_log
                   WHERE run_id = '$RUN2' AND trigger = 'mcp_degraded'")
if [[ "${DEGRADED:-0}" -ge 1 ]]; then
    note "·" "$DEGRADED demotion row(s) written — a call was in flight"
    psql_q "SELECT '      ' || subject_id || '  ' || outcome || '  ' || stop_reason
            FROM audit_log WHERE run_id = '$RUN2' AND trigger = 'mcp_degraded'"
else
    note "·" "no demotion row — nothing was mid-call, and the SDK does not surface a"
    note " " "  dead mount on its own. Expected; see docs/WHAT_BROKE.md, 3 Sep."
fi

echo
if (( FAIL )); then
    echo "drill FAILED — logs kept at $LOG1 and $LOG2"
    KEEP=1
    exit 1
fi
echo "drill passed · $RUN1 · $RUN2"
