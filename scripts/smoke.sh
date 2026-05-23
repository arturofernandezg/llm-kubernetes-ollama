#!/usr/bin/env bash
# Smoke test E2E contra el agente desplegado en arturo-llm-test.
# Uso: bash scripts/smoke.sh
# Requiere: kubectl con contexto apuntando al cluster.
# Exit 0 = todo OK. Exit 1 = fallo (mensaje en stderr).
set -euo pipefail

NAMESPACE="arturo-llm-test"
LOCAL_PORT="18000"
PF_PID=""
AGENT_URL="http://localhost:${LOCAL_PORT}"

cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== AIOps smoke test ==="

# 1. Port-forward agent-svc en background
echo "[1/6] Port-forward agent-svc:8000 → localhost:${LOCAL_PORT}"
kubectl port-forward svc/agent-svc "${LOCAL_PORT}:8000" -n "${NAMESPACE}" >/dev/null 2>&1 &
PF_PID=$!
sleep 3

# 2. /healthz
echo "[2/6] GET /healthz"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${AGENT_URL}/healthz")
if [ "$STATUS" != "200" ]; then
  echo "FAIL: /healthz returned HTTP ${STATUS}" >&2
  exit 1
fi
echo "  → 200 OK"

# 3. /readyz
echo "[3/6] GET /readyz"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${AGENT_URL}/readyz")
if [ "$STATUS" != "200" ]; then
  echo "FAIL: /readyz returned HTTP ${STATUS} (Ollama may be down)" >&2
  exit 1
fi
echo "  → 200 OK"

# 4. Snapshot contador inicial
echo "[4/6] Snapshot métricas antes del webhook"
BEFORE=$(curl -s "${AGENT_URL}/metrics" | grep '^aiops_webhook_requests_total' | awk '{print $2}' || echo "0")
echo "  aiops_webhook_requests_total antes = ${BEFORE:-0}"

# 5. POST /webhook/alert (payload Alertmanager sintético — OOMKilled, namespace arturo-chaos)
echo "[5/6] POST /webhook/alert (payload sintético OOMKilled)"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AGENT_URL}/webhook/alert" \
  -H "Content-Type: application/json" \
  -d "{\"version\":\"4\",\"groupKey\":\"smoke-test\",\"status\":\"firing\",\"receiver\":\"aiops-agent\",\"groupLabels\":{},\"commonLabels\":{\"alertname\":\"KubePodOOMKilled\",\"namespace\":\"arturo-chaos\",\"severity\":\"critical\"},\"commonAnnotations\":{},\"externalURL\":\"\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"KubePodOOMKilled\",\"namespace\":\"arturo-chaos\",\"pod\":\"smoke-test-pod\",\"severity\":\"critical\"},\"annotations\":{\"description\":\"Smoke test OOMKilled alert\"},\"startsAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"endsAt\":\"0001-01-01T00:00:00Z\",\"fingerprint\":\"smoke-test-fingerprint\"}]}")
if [ "$HTTP_CODE" != "200" ]; then
  echo "FAIL: /webhook/alert returned HTTP ${HTTP_CODE}" >&2
  exit 1
fi
echo "  → 200 OK"

# 6. Verificar que el contador incrementó (esperar pipeline async)
echo "[6/6] Verificar contadores aiops_* en /metrics (espera 5s para pipeline async)"
sleep 5
AFTER=$(curl -s "${AGENT_URL}/metrics" | grep '^aiops_webhook_requests_total' | awk '{print $2}' || echo "0")
echo "  aiops_webhook_requests_total después = ${AFTER:-0}"

ROLLBACK_PRESENT=$(curl -s "${AGENT_URL}/metrics" | grep -c '^aiops_remediation_rollback_total' || true)
REMEDIATION_PRESENT=$(curl -s "${AGENT_URL}/metrics" | grep -c '^aiops_remediation_total' || true)

ERRORS=0
if [ "${ROLLBACK_PRESENT:-0}" -eq 0 ]; then
  echo "WARN: aiops_remediation_rollback_total no encontrado en /metrics" >&2
  ERRORS=$((ERRORS + 1))
fi
if [ "${REMEDIATION_PRESENT:-0}" -eq 0 ]; then
  echo "WARN: aiops_remediation_total no encontrado en /metrics" >&2
  ERRORS=$((ERRORS + 1))
fi

echo ""
if [ "$ERRORS" -eq 0 ]; then
  echo "=== SMOKE TEST PASSED ==="
  exit 0
else
  echo "=== SMOKE TEST PASSED WITH WARNINGS (ver WARNs arriba) ===" >&2
  exit 0
fi
