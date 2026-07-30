#!/usr/bin/env bash
# Drive a sandboxed Home Assistant with an offline fake bank.
#
#   run.sh bootstrap   install HA component deps into $SB_ROOT/extra (once, needs net)
#   run.sh sandbox     (re)create the synthetic config sandbox, pre-migration state
#   run.sh start       start HA in the background
#   run.sh token       onboard (first time) / log in, write $SB_ROOT/token
#   run.sh stop        stop HA
#   run.sh up          sandbox + start + token
#   run.sh restart     stop + start (keeps .storage: entry already migrated)
#   run.sh api PATH [curl args...]   authenticated REST call
#
# Everything lives under $SB_ROOT (default: a scratch dir). The developer's own
# config/ is never read or written.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
HARNESS="$REPO/.claude/skills/verify/harness"
SB_ROOT="${SB_ROOT:-${TMPDIR:-/tmp}/fints-verify}"
SB="$SB_ROOT/config"
PORT="${SB_PORT:-8199}"
PY="$REPO/.venv/bin/python"
HASS="$REPO/.venv/bin/hass"
BASE="http://127.0.0.1:$PORT"
mkdir -p "$SB_ROOT" "$SB_ROOT/flags"
export SB_ROOT SB_PORT="$PORT"

# HA core is installed without its component requirements; get_services imports
# every base component, so a missing one leaves the UI stuck on "Loading data".
# hassil must match HA's pin (3.10 dropped hassil.fuzzy).
DEPS=(home-assistant-frontend==20260312.0 hassil==3.5.0
      home-assistant-intents==2026.3.3 mutagen PyTurboJPEG ha-ffmpeg
      pymicro-vad pyspeex-noise)

cmd_bootstrap() {
  "$REPO/.venv/bin/pip" install --quiet --upgrade --target "$SB_ROOT/extra" "${DEPS[@]}"
  echo "deps installed into $SB_ROOT/extra"
}

cmd_sandbox() { SB="$SB" "$PY" "$HARNESS/make_sandbox.py" "${@:-}"; rm -f "$SB_ROOT"/flags/*; }

cmd_start() {
  [ -d "$SB_ROOT/extra" ] || { echo "run '$0 bootstrap' first"; exit 1; }
  PYTHONPATH="$REPO:$HARNESS/fake_fints:$SB_ROOT/extra" \
  FAKE_FINTS=1 FAKE_FINTS_LOG="$SB_ROOT/fakebank.log" \
  FAKE_FINTS_FLAGDIR="$SB_ROOT/flags" \
    nohup "$HASS" --config "$SB" --skip-pip --log-file "$SB_ROOT/ha.log" \
    > "$SB_ROOT/ha.stdout" 2>&1 &
  echo $! > "$SB_ROOT/ha.pid"
  for _ in $(seq 1 20); do
    sleep 5
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/")" != "000" ]; then
      echo "HA up on $BASE (pid $(cat "$SB_ROOT/ha.pid"))"; return 0
    fi
  done
  echo "HA did not come up; see $SB_ROOT/ha.log"; return 1
}

# NEVER pkill -f hass: the pattern matches this script's own command line.
cmd_stop() {
  [ -f "$SB_ROOT/ha.pid" ] && kill "$(cat "$SB_ROOT/ha.pid")" 2>/dev/null || true
  sleep 4; rm -f "$SB_ROOT/ha.pid"; echo "stopped"
}

cmd_token() {
  local code
  if [ "$(curl -s "$BASE/api/onboarding" | grep -c '"step":"user","done":false')" -gt 0 ]; then
    code=$(curl -s -X POST "$BASE/api/onboarding/users" -H 'Content-Type: application/json' \
      -d "{\"client_id\":\"$BASE/\",\"name\":\"Sandbox Dev\",\"username\":\"dev\",\"password\":\"sandboxdevpw\",\"language\":\"de\"}" \
      | "$PY" -c "import sys,json;print(json.load(sys.stdin)['auth_code'])")
  else
    code=$(curl -s -X POST "$BASE/auth/login_flow" -H 'Content-Type: application/json' \
      -d "{\"client_id\":\"$BASE/\",\"handler\":[\"trusted_networks\",null],\"redirect_uri\":\"$BASE/\"}" \
      | "$PY" -c "import sys,json;print(json.load(sys.stdin)['result'])")
  fi
  curl -s -X POST "$BASE/auth/token" \
    -d "grant_type=authorization_code&code=$code&client_id=$BASE/" \
    | "$PY" -c "import sys,json,os;d=json.load(sys.stdin);open('$SB_ROOT/token','w').write(d['access_token']);print('token ok, expires_in',d['expires_in'])"
  # finish onboarding so the UI is reachable in a browser
  local tok; tok=$(cat "$SB_ROOT/token")
  for step in core_config analytics; do
    curl -s -X POST -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
      -d '{}' "$BASE/api/onboarding/$step" > /dev/null || true
  done
  curl -s -X POST -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"$BASE/\",\"redirect_uri\":\"$BASE/\"}" \
    "$BASE/api/onboarding/integration" > /dev/null || true
}

cmd_api() { local p="$1"; shift; curl -s -H "Authorization: Bearer $(cat "$SB_ROOT/token")" "$@" "$BASE$p"; }

case "${1:-}" in
  bootstrap) cmd_bootstrap ;;
  sandbox)   shift; cmd_sandbox "$@" ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  token)     cmd_token ;;
  up)        cmd_sandbox; cmd_start; cmd_token ;;
  restart)   cmd_stop; cmd_start ;;
  api)       shift; cmd_api "$@" ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
