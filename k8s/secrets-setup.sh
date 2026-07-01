#!/usr/bin/env bash
# =============================================================================
# Creacion de Secrets de Kubernetes para el proyecto AIOps
#
# IMPORTANTE: Este script contiene instrucciones con valores placeholder.
#             Sustituir <...> por los valores reales antes de ejecutar.
#             NO commitear este fichero con valores reales.
#
# Ejecutar desde GCloud Shell:
#   chmod +x k8s/secrets-setup.sh
#   bash k8s/secrets-setup.sh
# =============================================================================

set -euo pipefail

echo "=== Creando secrets para AIOps Agent ==="

# --- 1a. Secret del webhook URL de Mattermost (namespace: arturo-llm-test) ---
# El deployment lo lee de: Secret `mattermost-webhook`, clave `url` (MATTERMOST_WEBHOOK_URL).
# Obtener el token desde: Mattermost → Integrations → Incoming Webhooks
MATTERMOST_WEBHOOK_URL="http://mattermost-svc.arturo-mattermost.svc.cluster.local:8065/hooks/<TU-TOKEN-AQUI>"

kubectl create secret generic mattermost-webhook --from-literal=url="${MATTERMOST_WEBHOOK_URL}" -n arturo-llm-test --dry-run=client -o yaml | kubectl apply -f -

echo "[OK] mattermost-webhook creado en arturo-llm-test"

# --- 1b. Secret de auth del agente (namespace: arturo-llm-test) --------------
# El deployment lo lee de: Secret `agent-secrets`, claves `webhook-secret` + `mm-command-token`.
#   webhook-secret : firma HMAC de los botones de escalación (genera uno aleatorio).
#   mm-command-token: token que Mattermost genera al crear el slash command /aiops.
# IMPORTANTE (P0·2): con REMEDIATION_DRY_RUN=false, si estas claves faltan el agente
# rechaza los callbacks (fail-closed) — NO se puede aprobar ni ejecutar sin ellas.
WEBHOOK_SECRET="$(openssl rand -hex 32)"
MM_COMMAND_TOKEN="<TOKEN-DEL-SLASH-COMMAND-AQUI>"

kubectl create secret generic agent-secrets --from-literal=webhook-secret="${WEBHOOK_SECRET}" --from-literal=mm-command-token="${MM_COMMAND_TOKEN}" -n arturo-llm-test --dry-run=client -o yaml | kubectl apply -f -

echo "[OK] agent-secrets creado en arturo-llm-test (webhook-secret + mm-command-token)"

# --- 2. Secret de Mattermost (namespace: arturo-mattermost) -----------------
# Ya deberia existir (creado en el setup inicial de mattermost.yaml).
# Si necesitas recrearlo:
#
# DB_PASSWORD="<TU-PASSWORD-DB>"
# kubectl create secret generic mattermost-secrets \
#   --from-literal=db-password="${DB_PASSWORD}" \
#   --from-literal=db-datasource="postgres://mmuser:${DB_PASSWORD}@postgres-svc:5432/mattermost?sslmode=disable" \
#   -n arturo-mattermost \
#   --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "=== Secrets configurados ==="
echo "Verificar: kubectl get secrets -n arturo-llm-test"
echo "Verificar: kubectl get secrets -n arturo-mattermost"
