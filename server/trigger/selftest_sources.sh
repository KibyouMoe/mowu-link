#!/usr/bin/env bash
# Regression test for source declarations, schema check, health judgement and
# dedupe keys (0.5.1 feature set; kept as a gate for 0.6.0). L2 is off on
# purpose: the temp server gets a rule file that does not exist.
# Uses its own port + DB; never touches the production 8791 service.
#
#   ./selftest_sources.sh            # port 18791, db /tmp/t5.db
#   PORT=18800 DB=/tmp/x.db ./selftest_sources.sh
set -u
cd "$(dirname "$0")" || exit 1

PORT="${PORT:-18791}"
DB="${DB:-/tmp/t5.db}"
LOG="${LOG:-/tmp/t5.log}"
RULES="${RULES:-/tmp/t5-rules.json}"
BLOCKED="${BLOCKED:-/tmp/t5-blocked.jsonl}"
BASE="http://127.0.0.1:$PORT"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.hermes/secrets/trigger.token}"
BODY=/tmp/selftest_sources.body
OUT=/tmp/selftest_sources.out
PY=python3
pass=0
fail=0

check() {  # check <label> <exit-status>
    if [ "$2" -eq 0 ]; then
        echo "PASS  $1"; pass=$((pass + 1))
    else
        echo "FAIL  $1"; fail=$((fail + 1))
    fi
}

post() {  # post <json> -> prints HTTP status
    curl -sS --noproxy '*' -o "$BODY" -w '%{http_code}' \
        -X POST "$BASE/trigger" -H 'Content-Type: application/json' -d "$1"
}

json_check() {  # json_check <python expression over d>
    "$PY" -c "import json,sys; d=json.load(open('$BODY')); assert $1, d"
}

cleanup() {
    if [ -n "${PID:-}" ]; then
        kill "$PID" 2>/dev/null
        wait "$PID" 2>/dev/null
    fi
}
trap cleanup EXIT

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: port $PORT is already in use; pick another with PORT=" >&2
    exit 2
fi

rm -f "$DB" "$DB-wal" "$DB-shm" "$LOG" "$RULES" "$BLOCKED"
"$PY" server.py --port "$PORT" --db "$DB" --log "$LOG" \
    --rules-file "$RULES" --blocked-log "$BLOCKED" >"$OUT" 2>&1 &
PID=$!
up=1
for _ in $(seq 1 50); do
    if curl -sS --noproxy '*' "$BASE/health" >/dev/null 2>&1; then up=0; break; fi
    sleep 0.1
done
check "server up on 127.0.0.1:$PORT" "$up"

# 1. declarations are listed
curl -sS --noproxy '*' "$BASE/sources" >"$BODY"
json_check '{e["name"] for e in d} == {"email", "mc_chat"}'
check "GET /sources lists mc_chat + email" $?
json_check 'all(e["declared"] for e in d) and {e["mode"] for e in d} == {"push", "pull"}'
check "GET /sources exposes mode/declared" $?

# 2. valid declared event
CODE=$(post '{"source":"mc_chat","text":"hello from selftest","actor":"Steve","event_id":"t5-evt-1","dedupe_key":"t5-dedupe-1"}')
[ "$CODE" = "200" ] && json_check 'd["duplicate"] is False and d["dedupe_key"] == "t5-dedupe-1"'
check "valid mc_chat event -> 200, duplicate=false" $?
FIRST_ID=$("$PY" -c "import json;print(json.load(open('$BODY'))['id'])")

# 3. same dedupe_key, new event_id -> duplicate with the first id
CODE=$(post '{"source":"mc_chat","text":"hello again","actor":"Steve","event_id":"t5-evt-2","dedupe_key":"t5-dedupe-1"}')
[ "$CODE" = "200" ] && json_check "d['duplicate'] is True and d['id'] == $FIRST_ID"
check "same dedupe_key -> duplicate:true, same id" $?

# 4. schema violations
CODE=$(post '{"source":"mc_chat","text":123,"actor":"Steve"}')
[ "$CODE" = "400" ] && json_check 'd["error"] == "schema_mismatch" and d["field"] == "text"'
check "text=int -> 400 schema_mismatch field=text" $?

CODE=$(post '{"source":"mc_chat","text":"nobody"}')
[ "$CODE" = "400" ] && json_check 'd["error"] == "schema_mismatch" and d["field"] == "actor"'
check "missing actor -> 400 schema_mismatch field=actor" $?

# 5. undeclared source is accepted, hidden from /sources, visible in /stats
CODE=$(post '{"source":"selftest","text":"undeclared source","event_id":"t5-evt-3"}')
[ "$CODE" = "200" ]
check "undeclared source accepted -> 200" $?
curl -sS --noproxy '*' "$BASE/sources" >"$BODY"
json_check 'not any(e["name"] == "selftest" for e in d)'
check "GET /sources does not list selftest" $?
curl -sS --noproxy '*' -H "X-Trigger-Token: $(cat "$TOKEN_FILE")" \
    "$BASE/stats?subscriber=selftest" >"$BODY"
json_check 'any(e["name"] == "selftest" and e["declared"] is False and e["health"]["last_event_at"] for e in d["sources"])'
check "GET /stats shows selftest as declared=false" $?

# 6. the unique index is (dedupe_key, source), not dedupe_key alone
CODE=$(post '{"source":"selftest","text":"same key, other source","dedupe_key":"t5-dedupe-1"}')
[ "$CODE" = "200" ] && json_check 'd["duplicate"] is False'
check "same dedupe_key on another source -> accepted" $?

# 7. /health keeps its old fields and gains version + sources + rules
curl -sS --noproxy '*' "$BASE/health" >"$BODY"
json_check 'd["version"] == "0.7.0" and d["sources"] == 2 and "events" in d and "subscriptions" in d'
check "GET /health version=0.7.0 sources=2" $?
json_check 'd["rules"] == 0 and d["rules_mtime"] is None'
check "GET /health rules=0 (no rule file)" $?

# 8. shutdown releases the port
kill "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
PID=""
released=1
for _ in $(seq 1 50); do
    if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then released=0; break; fi
    sleep 0.1
done
check "port $PORT released after SIGTERM" "$released"

echo "-------------------------------------------"
echo "$pass passed, $fail failed  (port=$PORT db=$DB log=$LOG)"
[ "$fail" -eq 0 ] || exit 1
