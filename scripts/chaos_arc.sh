#!/usr/bin/env bash
# Chaos ARC runner — arco completo OOM → escalate → approve humano → veredicto (cured/rolled_back).
#
# Complementa (NO sustituye) a scripts/chaos.sh:
#   - chaos.sh  → mide MTTD/MTTR y limpia a los ~5min (NO sirve para el arco: lo corta a mitad, C-04).
#   - chaos_arc.sh → acompaña el arco entero (~10-15min con approve humano) y garantiza el TEARDOWN.
#
# Lección 2026-07-06 ("el horno nocturno"): un target curado a 512Mi olvidado quemó CPU ~36h.
# El teardown NO puede depender de la memoria humana → aquí es un `trap EXIT`: pase lo que pase
# (veredicto, timeout, Ctrl-C), el deployment se borra y las claves Redis del run se limpian.
#
# Uso: ./scripts/chaos_arc.sh [--keep]
#   --keep   No borrar el deployment al salir (inspección manual). Las claves Redis SÍ se limpian.
#
# El approve es humano: cuando el script avise, pulsa "Aprobar" en Mattermost (#alerts).

set -euo pipefail

NS_CHAOS="arturo-chaos"
NS_AGENT="arturo-llm-test"
CHAOS_DIR="$(cd "$(dirname "$0")/.." && pwd)/k8s/chaos"
MANIFEST="$CHAOS_DIR/chaos-oom.yaml"
DEPLOY="chaos-oom-target"
POLL_INTERVAL=10
# Presupuesto del arco: alerta (~1-2min) + LLM (~3-4min) + approve humano + ventana rollback (300s) + margen.
MAX_ARC_WAIT=2100
KEEP_TARGET=false
[ "${1:-}" = "--keep" ] && KEEP_TARGET=true

_agent_pod() {
    kubectl get pods -n "$NS_AGENT" -l app=agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# Borra en Redis las claves de estado del run. Patrones acotados a los prefijos del agente;
# OJO: escalation:*/rollback:* no distinguen chaos de real — en este sandbox es aceptable
# (mismo cleanup manual que hacíamos en los runs 07-04/07-06).
_redis_clean() {
    local pattern="$1"
    kubectl exec -n "$NS_AGENT" deploy/redis -- sh -c \
        "redis-cli --scan --pattern '$pattern' | xargs -r redis-cli del" 2>/dev/null || \
        echo "[arc] WARN: no se pudo limpiar '$pattern' en Redis (¿pod redis caído?)" >&2
}

# TEARDOWN — último gate del run, garantizado por trap. Consecuencia directa del horno nocturno.
_teardown() {
    local exit_code=$?
    echo ""
    echo "[arc] ── TEARDOWN (trap EXIT, exit_code=$exit_code) ──"
    if [ "$KEEP_TARGET" = true ]; then
        echo "[arc] --keep: se conserva el deployment $DEPLOY (bórralo tú: kubectl delete deployment -n $NS_CHAOS $DEPLOY)"
    else
        kubectl delete deployment -n "$NS_CHAOS" "$DEPLOY" --ignore-not-found
    fi
    echo "[arc] Limpiando estado del run en Redis (escalation:* rollback:* aiops:cooldown:*)..."
    _redis_clean "escalation:*"
    _redis_clean "rollback:*"
    _redis_clean "aiops:cooldown:*"
    echo "[arc] Teardown completado."
}
trap _teardown EXIT

echo "========================================================"
echo " Chaos ARC: OOM → escalate → approve → veredicto"
echo "========================================================"

# ── Pre-vuelo (checklist de los runs 07-04/07-06) ────────────────────────────
echo "[arc] Pre-vuelo..."
AGENT_POD=$(_agent_pod)
if [ -z "$AGENT_POD" ]; then
    echo "[arc] ERROR: no hay pod del agente en $NS_AGENT" >&2
    exit 1
fi
IMAGE=$(kubectl get deploy agent -n "$NS_AGENT" -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "[arc]   agente: $AGENT_POD ($IMAGE)"

READY=$(kubectl exec -n "$NS_AGENT" "$AGENT_POD" -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/readyz',timeout=5).status)" 2>/dev/null || echo "ERR")
if [ "$READY" != "200" ]; then
    echo "[arc] ERROR: /readyz devolvió '$READY' (esperado 200)" >&2
    exit 1
fi
echo "[arc]   /readyz: 200"

RESIDUAL=$(kubectl exec -n "$NS_AGENT" deploy/redis -- sh -c \
    "redis-cli --scan --pattern 'escalation:*'; redis-cli --scan --pattern 'rollback:*'; redis-cli --scan --pattern 'aiops:cooldown:*'" 2>/dev/null | grep -c . || true)
if [ "${RESIDUAL:-0}" -gt 0 ]; then
    echo "[arc]   WARN: $RESIDUAL clave(s) residual(es) de un run anterior — se limpian ahora (pre-vuelo limpio)"
    _redis_clean "escalation:*"; _redis_clean "rollback:*"; _redis_clean "aiops:cooldown:*"
else
    echo "[arc]   Redis sin residuos (cooldown/rollback/escalaciones)"
fi

# ── Lanzar el experimento ─────────────────────────────────────────────────────
echo "[arc] Aplicando $MANIFEST (stress infinito + --vm-hang 0)..."
kubectl apply -f "$MANIFEST"
T0=$(date +%s)
T0_ISO=$(date -u -d "@$T0" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$T0" +%Y-%m-%dT%H:%M:%SZ)
echo "[arc] T0 = $T0_ISO"
echo ""
echo "[arc] ➤ El arco tarda ~10-15min. Cuando llegue la escalación a Mattermost (#alerts),"
echo "[arc]   pulsa APROBAR (el comando determinista del motor, p.ej. --limits=memory=512Mi)."
echo ""

# ── Esperar el veredicto ──────────────────────────────────────────────────────
# La señal es el log del re-upsert R2: "Persisted incident <doc_id> (outcome=cured|rolled_back|...)".
# Se acepta cualquier veredicto final: el objetivo del ARC es que el bucle cierre, no que cure.
echo "[arc] Esperando veredicto (cured|rolled_back|rollback_failed) en logs del agente (max ${MAX_ARC_WAIT}s)..."
WAITED=0
VERDICT=""
while [ "$WAITED" -lt "$MAX_ARC_WAIT" ]; do
    VERDICT=$(kubectl logs -n "$NS_AGENT" "$AGENT_POD" --since-time="$T0_ISO" 2>/dev/null \
        | grep -oE 'outcome=(cured|rolled_back|rollback_failed)' | tail -1 | cut -d= -f2 || true)
    if [ -n "$VERDICT" ]; then
        break
    fi
    sleep "$POLL_INTERVAL"
    WAITED=$((WAITED + POLL_INTERVAL))
    if [ $((WAITED % 60)) -eq 0 ]; then
        echo "[arc]   ...${WAITED}s (recordatorio: ¿aprobaste ya en Mattermost?)"
    fi
done

echo ""
echo "-------- RESULTADO DEL ARCO --------"
if [ -n "$VERDICT" ]; then
    ELAPSED=$(( $(date +%s) - T0 ))
    printf "Veredicto        : %s\n" "$VERDICT"
    printf "Duración del arco: %ss desde T0\n" "$ELAPSED"
    echo ""
    echo "[arc] Verifica el counter (port-forward o exec):"
    echo "  kubectl exec -n $NS_AGENT $AGENT_POD -- python -c \"import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics').read().decode().splitlines() if 'feedback_verdict' in l])\""
else
    echo "Veredicto        : TIMEOUT (${MAX_ARC_WAIT}s) — ¿faltó el approve? ¿alerta no disparó?"
    echo "[arc] El teardown limpia igualmente (nada queda encendido)."
fi
echo "------------------------------------"
# El trap EXIT ejecuta el teardown aquí.
