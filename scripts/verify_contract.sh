#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/bad always return 0, so `cond && ok || bad` is safe
# Curl-based API contract verification (API_SPEC.md / assignment §3–4).
#
#   scripts/verify_contract.sh [BASE_URL] [FRONTEND_ORIGIN]
#   BASE_URL defaults to http://localhost:8000
#
# Requires: curl, jq. The backend must run with ENABLE_FAILURE_SIMULATION=true and >= 2 keys.
# Every run uses fresh session ids and messages, so earlier runs cannot pre-warm the cache.
set -uo pipefail

BASE="${1:-http://localhost:8000}"
ORIGIN="${2:-http://localhost:3000}"
RUN="$(date +%s)-$RANDOM"
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check() { # check "<description>" "<jq boolean expression>" "<json>"
  if echo "$3" | jq -e "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "$(echo "$3" | head -c 400)"; fi
}

# request METHOD PATH [BODY] [EXTRA_HEADER] → sets $CODE and $BODY
request() {
  local method="$1" path="$2" data="${3:-}" header="${4:-}"
  local args=(-s -o /tmp/vc_body -w '%{http_code}' -X "$method" "$BASE$path")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' --data-binary "$data")
  [ -n "$header" ] && args+=(-H "$header")
  CODE="$(curl "${args[@]}")"
  BODY="$(cat /tmp/vc_body)"
}
chat_body() { jq -cn --arg s "$1" --arg m "$2" '{session_id: $s, message: $m}'; }

echo "Contract verification against $BASE (run $RUN)"

echo "GET /health"
request GET /health
[ "$CODE" = 200 ] && ok "200" || bad "200 expected, got $CODE" "$BODY"
check "status ok, keys_configured >= 2 (int), cache_enabled true" \
  '.status == "ok" and (.keys_configured|type) == "number" and .keys_configured >= 2 and .cache_enabled == true' "$BODY"

S1="vc-a-$RUN"; S2="vc-b-$RUN"; Q="What is the capital of France? [$RUN]"

echo "POST /chat — live inference"
request POST /chat "$(chat_body "$S1" "$Q")"
[ "$CODE" = 200 ] && ok "200" || bad "200 expected, got $CODE" "$BODY"
LIVE="$BODY"
check "exact top-level fields + session echo" \
  "(keys == [\"metadata\",\"reply\",\"session_id\"]) and .session_id == \"$S1\" and (.reply|length) > 0" "$LIVE"
check "metadata fields and types" \
  '.metadata | (.resolution_source == "LIVE_INFERENCE") and (.model|type)=="string"
   and (.latency_ms|type)=="number" and (.latency_ms|floor)==.latency_ms
   and (.input_tokens|type)=="number" and .input_tokens > 0
   and (.output_tokens|type)=="number" and .output_tokens > 0
   and (.key_id|test("^key_[0-9]+$")) and .fallback_attempts == 0' "$LIVE"

echo "POST /chat — same first message, new session → cache"
request POST /chat "$(chat_body "$S2" "$Q")"
HIT="$BODY"
check "LLM_CACHE, key_id null, fallback_attempts 0" \
  '.metadata.resolution_source == "LLM_CACHE" and .metadata.key_id == null and .metadata.fallback_attempts == 0' "$HIT"
check "identical reply" ".reply == $(echo "$LIVE" | jq '.reply')" "$HIT"
check "stored usage returned (same tokens)" \
  ".metadata.input_tokens == $(echo "$LIVE" | jq '.metadata.input_tokens') and .metadata.output_tokens == $(echo "$LIVE" | jq '.metadata.output_tokens')" "$HIT"
check "cache hit faster than live" ".metadata.latency_ms < $(echo "$LIVE" | jq '.metadata.latency_ms')" "$HIT"

echo "POST /chat — different message is never cached"
request POST /chat "$(chat_body "$S2" "And of Spain? [$RUN]")"
check "LIVE_INFERENCE" '.metadata.resolution_source == "LIVE_INFERENCE"' "$BODY"

echo "POST /chat — X-Simulate-Failure"
for h in "key_1=429" "key_1=500"; do
  request POST /chat "$(chat_body "vc-sim-$h-$RUN" "Simulated $h [$RUN]")" "X-Simulate-Failure: $h"
  check "$h → 200, key_2, fallback_attempts 1, real reply" \
    '.metadata.key_id == "key_2" and .metadata.fallback_attempts == 1 and (.reply|length) > 0' "$BODY"
done
S3="vc-dead-$RUN"
request POST /chat "$(chat_body "$S3" "All down [$RUN]")" "X-Simulate-Failure: *=429"
[ "$CODE" = 503 ] && ok "*=429 → 503" || bad "*=429 → 503 expected, got $CODE" "$BODY"
check 'body {"detail":"all providers unavailable"}' '. == {"detail":"all providers unavailable"}' "$BODY"
request GET "/sessions/$S3/messages"
[ "$CODE" = 404 ] && ok "failed request stored nothing (history 404)" || bad "expected 404, got $CODE" "$BODY"

echo "GET /sessions/{id}/messages"
request GET "/sessions/$S2/messages"
[ "$CODE" = 200 ] && ok "200" || bad "200 expected, got $CODE" "$BODY"
check "oldest first, 4 messages, roles alternate, no system prompt" \
  ".session_id == \"$S2\" and ([.messages[].role] == [\"user\",\"assistant\",\"user\",\"assistant\"])
   and .messages[0].content == \"$Q\"" "$BODY"
check "metadata only on assistant messages" \
  '([.messages[] | select(.role=="user") | has("metadata")] | any | not)
   and ([.messages[] | select(.role=="assistant") | .metadata.resolution_source] | all)' "$BODY"
request GET "/sessions/vc-unknown-$RUN/messages"
[ "$CODE" = 404 ] && ok "unknown session → 404" || bad "404 expected, got $CODE"
check 'body {"detail":"session not found"}' '. == {"detail":"session not found"}' "$BODY"

echo "POST /chat — validation (422, JSON detail)"
for payload in '{"session_id":"x","message":""}' '{"session_id":"x","message":"   "}' \
               '{"message":"hi"}' '{"session_id":"","message":"hi"}' '{bad json'; do
  request POST /chat "$payload"
  if [ "$CODE" = 422 ] && echo "$BODY" | jq -e 'has("detail")' >/dev/null 2>&1; then
    ok "422 for $payload"; else bad "422 expected for $payload, got $CODE" "$BODY"; fi
done
LONG="$(head -c 4001 /dev/zero | tr '\0' 'x')"
request POST /chat "$(chat_body "x" "$LONG")"
[ "$CODE" = 422 ] && ok "422 for 4001-char message" || bad "422 expected, got $CODE"

echo "CORS"
PRE="$(curl -s -o /dev/null -D - -X OPTIONS "$BASE/chat" -H "Origin: $ORIGIN" \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-simulate-failure' | tr -d '\r')"
echo "$PRE" | grep -qi "^access-control-allow-origin: $ORIGIN" && ok "preflight allows $ORIGIN" || bad "preflight origin" "$PRE"
echo "$PRE" | grep -qi "^access-control-allow-headers:.*x-simulate-failure" && ok "preflight allows X-Simulate-Failure" || bad "preflight header" "$PRE"

echo
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
