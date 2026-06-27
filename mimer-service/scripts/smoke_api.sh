#!/usr/bin/env bash
#
# Post-deploy smoke test for the Mimer API. Verifies the API is up, can reach
# Postgres, is on the expected migration head, and that the key read endpoints
# return cleanly (including an empty/missing-data workspace). Read-only — it
# never ingests, never calls a live data source, never mutates the database.
#
# Base URL precedence:  MIMER_BASE_URL > BASE_URL > http://127.0.0.1:${API_PORT:-8080}
# (defaults to the localhost-only port the prod compose binds, which is exactly
# what the VPS's existing reverse proxy / Caddy proxies to).
#
# Auth: set MIMER_API_TOKEN to the same value the API was started with and the
# /api/v1 checks send  Authorization: Bearer <token>. Leave it blank/unset to
# smoke an unauthenticated (local dev) API. The /health and /health/db probes are
# always called WITHOUT auth (they are never token-protected). The token is only
# ever passed to curl as an argument — it is never printed.
#
# Usage:
#   scripts/smoke_api.sh                                   # localhost:8080, workspace 1
#   API_PORT=9000 scripts/smoke_api.sh                     # localhost:9000
#   MIMER_API_TOKEN='…' scripts/smoke_api.sh               # auth-enabled API
#   MIMER_BASE_URL=https://your-domain.example scripts/smoke_api.sh   # through Caddy
#   MIMER_BASE_URL=https://your.domain.example MIMER_API_TOKEN='…' scripts/smoke_api.sh
#   BASE_URL=http://10.0.0.5:8080 WORKSPACE_ID=2 scripts/smoke_api.sh
#
# Core checks (health, db, migration head, diagnostics) must pass. The
# workspace-scoped checks are SKIPPED (not failed) when the workspace does not
# exist yet — e.g. a fresh deploy before any data is seeded — so a clean,
# data-less box still reports a passing smoke. Seed (or pass WORKSPACE_ID) to
# exercise them.
#
# Exit code 0 = no failures (skips are OK); non-zero = at least one failed check.

set -euo pipefail

BASE_URL="${MIMER_BASE_URL:-${BASE_URL:-http://127.0.0.1:${API_PORT:-8080}}}"
WORKSPACE_ID="${WORKSPACE_ID:-1}"
EXPECTED_MIGRATION_HEAD="${EXPECTED_MIGRATION_HEAD:-0019}"

pass=0
fail=0
skip=0

# curl flags: -f fail on HTTP >=400, -s silent, -S show errors, max 10s.
# Three command arrays (each starts with `curl`, so it is never empty — safe to
# expand under `set -u`, including on macOS bash 3.2):
#   CURL     — unauthenticated, for the /health probes.
#   API_CURL — adds the Bearer header (when MIMER_API_TOKEN is set), for /api/v1.
#   WS_CURL  — like API_CURL but keeps non-2xx bodies (no -f) + appends the status
#              code, so workspace checks can tell 404-not-found from a real error.
# The token is held only inside these arrays — never echoed.
CURL=(curl -fsS --max-time 10)
API_CURL=(curl -fsS --max-time 10)
WS_CURL=(curl -sS --max-time 10 -w $'\n%{http_code}')
if [[ -n "${MIMER_API_TOKEN:-}" ]]; then
  API_CURL+=(-H "Authorization: Bearer ${MIMER_API_TOKEN}")
  WS_CURL+=(-H "Authorization: Bearer ${MIMER_API_TOKEN}")
fi

check() {
  # check "<label>" <command...>
  local label="$1"
  shift
  printf '  %-56s' "$label"
  if "$@" >/dev/null 2>&1; then
    echo "OK"
    pass=$((pass + 1))
  else
    echo "FAIL"
    fail=$((fail + 1))
  fi
}

# Assert a GET succeeds AND its body contains an expected substring (core check).
# /api/v1 URLs go through the auth-aware curl (Bearer header when MIMER_API_TOKEN
# is set); /health* URLs are probed without auth (they are never token-protected).
check_body() {
  local label="$1" url="$2" needle="$3"
  printf '  %-56s' "$label"
  local body
  local -a cmd
  if [[ "$url" == */api/* ]]; then cmd=("${API_CURL[@]}"); else cmd=("${CURL[@]}"); fi
  if body="$("${cmd[@]}" "$url" 2>/dev/null)" && [[ "$body" == *"$needle"* ]]; then
    echo "OK"
    pass=$((pass + 1))
  else
    echo "FAIL"
    fail=$((fail + 1))
  fi
}

# Workspace-scoped GET. SKIP (not FAIL) when the workspace does not exist yet
# (404 workspace_not_found) — a fresh, unseeded box is a clean state, not a
# broken one. Otherwise require HTTP 200 (and an optional body substring).
check_workspace() {
  local label="$1" url="$2" needle="${3:-}"
  printf '  %-56s' "$label"
  local resp code body
  # Append the status code on its own trailing line; split it back off so a body
  # containing newlines is handled correctly. WS_CURL carries the Bearer header
  # when MIMER_API_TOKEN is set (workspace endpoints live under /api/v1).
  resp="$("${WS_CURL[@]}" "$url" 2>/dev/null || true)"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [[ "$code" == "404" && "$body" == *"workspace_not_found"* ]]; then
    echo "SKIP (workspace ${WORKSPACE_ID} not found)"
    skip=$((skip + 1))
    return
  fi
  if [[ "$code" == "200" ]] && { [[ -z "$needle" ]] || [[ "$body" == *"$needle"* ]]; }; then
    echo "OK"
    pass=$((pass + 1))
  else
    echo "FAIL (HTTP ${code:-?})"
    fail=$((fail + 1))
  fi
}

echo "Mimer API smoke test"
echo "  base_url=${BASE_URL} workspace_id=${WORKSPACE_ID} expected_head=${EXPECTED_MIGRATION_HEAD}"
echo

# --- Core checks (must pass on any healthy deploy, data or not) ---

# 1. API liveness.
check_body "GET /health (API up)" \
  "${BASE_URL}/health" '"status":"ok"'

# 2. Database connectivity (readiness).
check_body "GET /health/db (database connected)" \
  "${BASE_URL}/health/db" '"database":"connected"'

# 3. Migration head is current (capabilities advertises the alembic head).
check_body "GET /api/v1/capabilities (migration head ${EXPECTED_MIGRATION_HEAD})" \
  "${BASE_URL}/api/v1/capabilities" "\"migration_head\":\"${EXPECTED_MIGRATION_HEAD}\""

# 4. Diagnostics endpoint works (DB-backed global counts; not workspace-scoped).
check_body "GET /api/v1/diagnostics" \
  "${BASE_URL}/api/v1/diagnostics" "due_scheduled_jobs"

# --- Workspace-scoped checks (skipped cleanly if the workspace is absent) ---

# 5. Market-data planner endpoint works.
check_workspace "GET /api/v1/workspaces/{id}/market-data-plan" \
  "${BASE_URL}/api/v1/workspaces/${WORKSPACE_ID}/market-data-plan" "\"workspace_id\":${WORKSPACE_ID}"

# 6. Dashboard aggregate works.
check_workspace "GET /api/v1/workspaces/{id}/dashboard" \
  "${BASE_URL}/api/v1/workspaces/${WORKSPACE_ID}/dashboard"

# 7. Portfolio valuation summary returns a clean payload (empty/missing state OK).
check_workspace "GET /api/v1/workspaces/{id}/portfolio/valuation/summary" \
  "${BASE_URL}/api/v1/workspaces/${WORKSPACE_ID}/portfolio/valuation/summary"

echo
echo "Passed: ${pass}  Failed: ${fail}  Skipped: ${skip}"
if [[ "$skip" -gt 0 ]]; then
  echo "Note: ${skip} workspace-scoped check(s) skipped — workspace ${WORKSPACE_ID} has no data yet."
  echo "      Seed it (docker compose run --rm api uv run python -m app.seed.seed_data)"
  echo "      or set WORKSPACE_ID to an existing workspace to exercise them."
fi
[[ "$fail" -eq 0 ]] || exit 1
echo "Smoke test passed."
