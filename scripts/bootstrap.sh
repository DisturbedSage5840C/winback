#!/usr/bin/env bash
# Winback — bring a fresh clone to a working state.
#
# Every failure below prints the actual remedy. The single most expensive minute in a
# five-minute demo is the one spent debugging a healthy config file because the tool
# reported the wrong thing (see docs/WHAT_BROKE.md, 2026-08-26).

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

say()  { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- prerequisites
command -v python3 >/dev/null || fail "python3 not found."
command -v docker  >/dev/null || fail "docker not found. Install Docker Desktop."

docker info >/dev/null 2>&1 || fail \
  "The Docker daemon is not running. Start Docker Desktop and re-run this script.
   (The compose file is fine — this failure reports itself as a socket error.)"
ok "docker daemon reachable"

# ---------------------------------------------------------------- env
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "created .env from .env.example (no Razorpay credentials needed for the batch lane)"
else
  ok ".env already present — left untouched"
fi

# ---------------------------------------------------------------- venv
if [[ ! -d .venv ]]; then
  say "creating virtualenv"
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
say "installing pinned dependencies (this takes a minute on a cold cache)"
./.venv/bin/pip install --quiet -r requirements.txt
ok "dependencies installed"

# ---------------------------------------------------------------- database
say "starting Postgres"
docker compose up -d >/dev/null

for _ in $(seq 1 40); do
  if docker exec winback-db pg_isready -U winback_owner -d winback >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker exec winback-db pg_isready -U winback_owner -d winback >/dev/null 2>&1 || fail \
  "Postgres did not become ready. Inspect with: docker logs winback-db"

# The schema only loads into an empty volume. A half-initialised volume is a confusing
# failure mode, so check for the tables rather than trusting that the container is up.
TABLES=$(docker exec winback-db psql -U winback_owner -d winback -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
# 11 is the whole schema: 7 fact/world tables plus the 4 eval_* tables. The floor used to
# be 8, which is the wrong kind of lenient — it passes a volume where the eval tables
# never got created and only tells you so a day later, from `python -m eval`.
if [[ "$TABLES" -lt 11 ]]; then
  fail "Postgres is up but the schema did not load fully (found $TABLES tables, expected 11).
   The init scripts only run against an empty volume. Reset with:
       docker compose down -v && docker compose up -d"
fi
ok "database ready — $TABLES tables, append-only DDL applied"

# ---------------------------------------------------------------- verify
say "verifying the audit trail is actually immutable"
./.venv/bin/python -m pytest core/tests/test_append_only.py -q

echo
ok "bootstrap complete"
echo "  next:  cd $REPO && ./.venv/bin/python -m pytest"
