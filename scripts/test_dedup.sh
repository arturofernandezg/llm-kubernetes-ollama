#!/usr/bin/env bash
# Test dedup in-flight: misma alerta dos veces seguidas
# Espera: req1=200 procesado, req2=200 skipeado, aiops_dedup_skipped_total=1
set -e

PORT=18001
NS=arturo-llm-test

PAYLOAD='{"version":"4","groupKey":"{}","status":"firing","receiver":"aiops-agent","groupLabels":{"alertname":"KubePodOOMKilled"},"commonLabels":{"alertname":"KubePodOOMKilled","namespace":"arturo-dedup-test","pod":"dedup-pod-x"},"commonAnnotations":{},"externalURL":"","alerts":[{"status":"firing","labels":{"alertname":"KubePodOOMKilled","namespace":"arturo-dedup-test","pod":"dedup-pod-x"},"annotations":{},"startsAt":"2026-05-27T00:00:00Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":""}]}'

echo "==> Starting port-forward on $PORT..."
kubectl port-forward svc/agent-svc $PORT:8000 -n $NS &>/dev/null &
PF_PID=$!
sleep 3

echo "==> req1 (debe procesar)..."
curl -s -o /dev/null -w "req1: %{http_code}\n" \
  -X POST localhost:$PORT/webhook/alert \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD"

echo "==> req2 (misma alerta — debe ser skipeada por dedup)..."
curl -s -o /dev/null -w "req2: %{http_code}\n" \
  -X POST localhost:$PORT/webhook/alert \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD"

sleep 2

echo "==> Métrica dedup:"
curl -s localhost:$PORT/metrics | grep dedup

echo "==> Log dedup (últimas 5 líneas):"
kubectl logs -n $NS deploy/agent --tail=5 2>/dev/null | grep -i "dedup\|in.flight\|skip" || echo "(ninguna línea dedup aún — el pipeline tarda ~200s)"

kill $PF_PID 2>/dev/null
echo "==> Done."
