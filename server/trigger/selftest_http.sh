#!/usr/bin/env bash
# HTTP self-test for mowu-trigger. Assumes the server is already running on
# 127.0.0.1:8791 with a disposable subscriber. Uses only curl + python3.
set -u
BASE="${BASE:-http://127.0.0.1:8791}"
SUB="${SUB:-hermes-selftest}"
PY=/usr/bin/python3
RUN_ID="$(date +%s)"
EVENT_B_ID="test-fixed-id-$RUN_ID"
say() { printf '\n===== %s =====\n' "$*"; }
json() { "$PY" -c 'import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }
field() { "$PY" -c 'import sys,json; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

say "0. health (before)"
curl -sS "$BASE/health" | json

say "1. POST /trigger event A"
A=$(curl -sS -X POST "$BASE/trigger" -H 'Content-Type: application/json' \
  -d '{"source":"mc_chat","kind":"chat","actor":"Steve","text":"hello from HTTP test"}')
echo "$A" | json
ID_A=$(echo "$A" | field id)

say "2. POST /trigger event B with custom event_id"
B=$(curl -sS -X POST "$BASE/trigger" -H 'Content-Type: application/json' \
  -d "{\"source\":\"email\",\"kind\":\"mail\",\"priority\":2,\"actor\":\"alice@example.com\",\"text\":\"Subject: build finished\",\"event_id\":\"$EVENT_B_ID\"}")
echo "$B" | json
ID_B=$(echo "$B" | field id)

say "3. POST same event_id again (dedupe expected)"
curl -sS -X POST "$BASE/trigger" -H 'Content-Type: application/json' \
  -d "{\"source\":\"email\",\"kind\":\"mail\",\"priority\":2,\"actor\":\"alice@example.com\",\"text\":\"Subject: build finished\",\"event_id\":\"$EVENT_B_ID\"}" | json

say "4. GET /events since=0 (order, unread only)"
curl -sS "$BASE/events?subscriber=$SUB&since=0&limit=50" | json

say "5. GET /events same since again (cursor must NOT advance)"
curl -sS "$BASE/events?subscriber=$SUB&since=0&limit=50" | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("count=%d ids=%s" % (len(d), [e["id"] for e in d]))'

say "6. POST /ack up_to_id=$ID_A, then GET since=0 (only B remains)"
curl -sS -X POST "$BASE/ack" -H 'Content-Type: application/json' \
  -d "{\"subscriber\":\"$SUB\",\"up_to_id\":$ID_A}" | json
curl -sS "$BASE/events?subscriber=$SUB&since=0&limit=50" | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("count=%d ids=%s texts=%s" % (len(d), [e["id"] for e in d], [e["text"] for e in d]))'

say "7. POST /ack up_to_id=$ID_B, then GET since=0 (empty)"
curl -sS -X POST "$BASE/ack" -H 'Content-Type: application/json' \
  -d "{\"subscriber\":\"$SUB\",\"up_to_id\":$ID_B}" | json
curl -sS "$BASE/events?subscriber=$SUB&since=0&limit=50" | json

say "8. /stats (unread should be 0)"
curl -sS "$BASE/stats?subscriber=$SUB" | json

say "9. long poll wait=5 with no events (wall clock)"
T=$(curl -sS -o /tmp/mowu-lp-empty.json -w '%{time_total}' \
  "$BASE/events?subscriber=$SUB&since=$ID_B&wait=5&limit=50")
echo "time_total=${T}s body=$(cat /tmp/mowu-lp-empty.json)"

say "10. long poll wake: wait=15, POST after 1s"
curl -sS -o /tmp/mowu-lp-wake.json -w '%{time_total}' \
  "$BASE/events?subscriber=$SUB&since=$ID_B&wait=15&limit=50" > /tmp/mowu-lp-wake.time &
CURL_PID=$!
sleep 1
C=$(curl -sS -X POST "$BASE/trigger" -H 'Content-Type: application/json' \
  -d '{"source":"mc_chat","kind":"chat","actor":"Alex","text":"wake up long poll"}')
ID_C=$(echo "$C" | field id)
wait $CURL_PID
echo "time_total=$(cat /tmp/mowu-lp-wake.time)s body=$(cat /tmp/mowu-lp-wake.json)"

say "11. cleanup ack for event C"
curl -sS -X POST "$BASE/ack" -H 'Content-Type: application/json' \
  -d "{\"subscriber\":\"$SUB\",\"up_to_id\":$ID_C}" | json
curl -sS "$BASE/stats?subscriber=$SUB" | json

say "12. health (after)"
curl -sS "$BASE/health" | json
