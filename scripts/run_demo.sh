#!/usr/bin/env bash
# Winback — one command from a fresh clone to a dashboard rendering real rows.
#
#   ./scripts/run_demo.sh              bring everything up, skipping work already done
#   ./scripts/run_demo.sh --reseed     wipe and rebuild the world from the frozen seed
#   ./scripts/run_demo.sh --no-ui      backend only (API on :8000, no npm, no browser)
#
# Two things this script deliberately does NOT do:
#
#   It does not reseed by default. `sim.generate --load` calls `reset_world()`, which
#   deletes the audit trail — including whatever batch you ran ten minutes ago and are
#   about to demo. Destroying an immutable-by-design log because someone re-ran the
#   start script would be a bad joke, so seeding happens only into an empty database
#   or behind an explicit --reseed.
#
#   It does not run in --live mode. The live lane creates real Razorpay artifacts and
#   is opt-in through `python -m agent.orchestrator --live`, never through the script
#   a stranger runs first.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

API_PORT="${API_PORT:-8000}"
UI_PORT="${PORT:-8443}"
RESEED=0
WITH_UI=1

for arg in "$@"; do
  case "$arg" in
    --reseed) RESEED=1 ;;
    --no-ui)  WITH_UI=0 ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown flag: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

PY="$REPO/.venv/bin/python"
psql_q() { docker exec winback-db psql -U winback_owner -d winback -tAc "$1"; }

# ---------------------------------------------------------------- prerequisites
[[ -x "$PY" ]] || fail "no virtualenv at .venv — run ./scripts/bootstrap.sh first."
command -v docker >/dev/null || fail "docker not found. Install Docker Desktop."
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running. Start Docker Desktop."

if (( WITH_UI )); then
  command -v npm >/dev/null || fail \
    "npm not found, and --no-ui was not passed. Install Node 20+, or run the backend
   alone with: ./scripts/run_demo.sh --no-ui"
fi

# ---------------------------------------------------------------- database
say "starting Postgres"
docker compose up -d >/dev/null
for _ in $(seq 1 40); do
  docker exec winback-db pg_isready -U winback_owner -d winback >/dev/null 2>&1 && break
  sleep 1
done
docker exec winback-db pg_isready -U winback_owner -d winback >/dev/null 2>&1 \
  || fail "Postgres did not become ready. Inspect with: docker logs winback-db"

# The init scripts only run against an empty volume, so a container that is up is not
# the same as a database that is loaded — check the tables, not the process.
TABLES=$(psql_q "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
(( TABLES >= 8 )) || fail \
  "Postgres is up but the schema did not load (found $TABLES tables).
   Reset with: docker compose down -v && ./scripts/bootstrap.sh"
ok "database ready — $TABLES tables"

# ---------------------------------------------------------------- seed
INVOICES=$(psql_q "SELECT count(*) FROM invoices")
if (( RESEED )) || (( INVOICES == 0 )); then
  if (( RESEED )) && (( INVOICES > 0 )); then
    say "--reseed: dropping the existing world and audit trail"
  fi
  say "generating the frozen dataset (500 subscriptions, seeded — same numbers every time)"
  "$PY" -m sim.generate --load
else
  ok "dataset already loaded — $INVOICES invoices (pass --reseed to rebuild)"
fi

# ---------------------------------------------------------------- model
if (( RESEED )) || [[ ! -f ml/artifacts/model_v1.json ]]; then
  say "training and calibrating model v1"
  "$PY" -m ml
else
  ok "model v1 artifacts present"
fi

# ---------------------------------------------------------------- batch
RUNS=$(psql_q "SELECT count(DISTINCT run_id) FROM audit_log")
if (( RESEED )) || (( RUNS == 0 )); then
  say "running the recovery batch (this is the agent loop — a few minutes)"
  "$PY" -m agent.orchestrator
  say "scoring the four arms"
  "$PY" -m eval
else
  ok "$RUNS runs already in the audit log (pass --reseed to re-run the batch)"
fi

# ---------------------------------------------------------------- serve
# One trap for both children: Ctrl-C in the foreground `wait` has to take the
# background uvicorn with it, or the next run of this script dies on a bound port.
PIDS=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

say "starting the API on :$API_PORT"
"$PY" -m uvicorn api.main:app --port "$API_PORT" --log-level warning &
PIDS+=($!)

for _ in $(seq 1 30); do
  curl -sf "http://localhost:$API_PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
HEALTH=$(curl -sf "http://localhost:$API_PORT/health" 2>/dev/null) \
  || fail "the API did not answer on :$API_PORT. Is something else using that port?"
ok "API up — $HEALTH"

if (( WITH_UI )); then
  if [[ ! -d dashboard/node_modules ]]; then
    say "installing dashboard dependencies (npm — a couple of minutes on a cold cache)"
    (cd dashboard && npm install --silent)
  fi
  say "starting the dashboard on :$UI_PORT"
  (cd dashboard && PORT="$UI_PORT" VITE_API_BASE="http://localhost:$API_PORT" npm run dev) &
  PIDS+=($!)

  for _ in $(seq 1 60); do
    curl -sf "http://localhost:$UI_PORT/" >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo
ok "Winback is up."
# `if`, not `(( WITH_UI )) && echo` — a false `&&` list is a failed command, and
# under `set -e` that exits the script one line before the useful output.
if (( WITH_UI )); then echo "     dashboard   http://localhost:$UI_PORT/"; fi
echo "     API docs    http://localhost:$API_PORT/docs"
echo
echo "  Ctrl-C stops both."
wait
