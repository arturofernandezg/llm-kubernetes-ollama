#!/usr/bin/env bash
# Chaos Engineering runner — Mini-Fase 4
# Uso: ./scripts/chaos.sh <oom|crashloop|status|cleanup>
#
# PREREQUISITO (oom): mirror de polinux/stress a AR:
#   crane copy --platform linux/amd64 polinux/stress:latest \
#     europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/polinux-stress:latest

set -euo pipefail

NS_CHAOS="arturo-chaos"
NS_AGENT="arturo-llm-test"
CHAOS_DIR="$(cd "$(dirname "$0")/.." && pwd)/k8s/chaos"
POLL_INTERVAL=5
MAX_POD_WAIT=180   # segundos esperando estado de fallo
MAX_AGENT_WAIT=600 # segundos esperando que el agente procese la alerta (HighCPU for:5m necesita ≥300s)

_usage() {
    echo "Uso: $0 <oom|crashloop|bad-image|cpu|status|cleanup>"
    echo "  oom        Experimento OOMKilled (requiere mirror de polinux/stress a AR)"
    echo "  crashloop  Experimento CrashLoopBackOff"
    echo "  bad-image  Experimento ImagePullBackOff (imagen inexistente -> alerta KubePodImagePullBackOff)"
    echo "  cpu        Experimento HighCPU (stress --cpu 2, limits 100m -> alerta HighCPU)"
    echo "  status     Ver pods en arturo-chaos"
    echo "  cleanup    Borrar namespace arturo-chaos"
    exit 1
}

_agent_pod() {
    kubectl get pods -n "$NS_AGENT" -l app=agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# Espera estado de fallo en un pod del deployment. Imprime mensajes a stderr.
# stdout: unix epoch cuando se detecta; retorno 1 si timeout.
_wait_for_pod_failure() {
    local label="$1" pattern="$2"
    local waited=0
    echo "[chaos] Esperando pod '$label' en estado '$pattern' (max ${MAX_POD_WAIT}s)..." >&2
    while [ "$waited" -lt "$MAX_POD_WAIT" ]; do
        local status
        status=$(kubectl get pods -n "$NS_CHAOS" -l "app=$label" --no-headers 2>/dev/null | awk '{print $3}' | grep -E "$pattern" || true)
        if [ -n "$status" ]; then
            echo "[chaos] Estado detectado: $status" >&2
            date +%s
            return 0
        fi
        sleep "$POLL_INTERVAL"
        waited=$((waited + POLL_INTERVAL))
    done
    echo "[chaos] TIMEOUT: pod no entró en '$pattern' en ${MAX_POD_WAIT}s" >&2
    return 1
}

# Convierte unix epoch a ISO 8601 UTC (RFC3339) para --since-time de kubectl.
# GNU date usa -d @N; BSD/macOS usa -r N. Retorna "" si ambos fallan.
_epoch_to_iso_utc() {
    local epoch="$1"
    date -u -d "@${epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo ""
}

# Espera hasta ver la alerta en los logs del agente, posteriores a T0.
# Args: <alertname> <t0_epoch>
# stdout: ISO timestamp del log cuando se detecta; retorno 1 si timeout.
_wait_for_agent_log() {
    local alertname="$1" t0_epoch="${2:-0}"
    local agent_pod waited=0 since_time log_flag
    agent_pod=$(_agent_pod)
    if [ -z "$agent_pod" ]; then
        echo "[chaos] ERROR: no se encontró pod del agente en $NS_AGENT" >&2
        return 1
    fi
    since_time=$(_epoch_to_iso_utc $((t0_epoch - 10)))
    if [ -n "$since_time" ]; then
        log_flag="--since-time=$since_time"
    else
        log_flag="--since=15m"
    fi
    echo "[chaos] Esperando log de '$alertname' en agente (max ${MAX_AGENT_WAIT}s, pod=$agent_pod, desde ${since_time:-15m atrás})..." >&2
    while [ "$waited" -lt "$MAX_AGENT_WAIT" ]; do
        local ts
        ts=$(kubectl logs -n "$NS_AGENT" "$agent_pod" $log_flag 2>/dev/null \
            | python3 -c "
import sys, json, re
from datetime import datetime, timezone
t0 = $t0_epoch
for line in sys.stdin:
    line=line.strip()
    if not line:
        continue
    try:
        d=json.loads(line)
        a=str(d.get('alertname',''))
        ev=str(d.get('event',''))
        if '$alertname' in a or '$alertname' in ev:
            t=d.get('timestamp','')
            if t:
                s=re.sub(r'[,.].*','',t)
                ep=int(datetime.strptime(s,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
                if ep >= t0:
                    print(t)
    except Exception:
        pass
" 2>/dev/null | grep -v '^$' | head -1 || true)
        if [ -n "$ts" ]; then
            echo "[chaos] Alerta detectada en logs: $ts" >&2
            echo "$ts"
            return 0
        fi
        sleep "$POLL_INTERVAL"
        waited=$((waited + POLL_INTERVAL))
    done
    echo "[chaos] TIMEOUT: agente no procesó '$alertname' en ${MAX_AGENT_WAIT}s" >&2
    return 1
}

# Convierte ISO timestamp a unix epoch (GNU date o python3 como fallback para macOS)
_iso_to_epoch() {
    date -d "$1" +%s 2>/dev/null || \
    python3 -c "import re; s=re.sub(r'[,.].*','','$1'); from datetime import datetime,timezone; print(int(datetime.strptime(s,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()))" 2>/dev/null || \
    echo "0"
}

# Espera el log "Chaos metrics recorded" del agente — confirma que is_chaos disparó y extrae MTTD/MTTR reales.
# Args: <alertname> <t0_epoch>
# stdout: "<mttd_s>|<mttr_s>|<outcome>" si se encuentra; retorno 1 si timeout (no-fatal, experimento continúa).
_wait_for_chaos_metrics() {
    local alertname="$1" t0_epoch="${2:-0}"
    local agent_pod waited=0 budget=300 since_time log_flag
    agent_pod=$(_agent_pod)
    if [ -z "$agent_pod" ]; then
        echo "[chaos] WARN: no se encontró pod del agente para métricas chaos" >&2
        return 1
    fi
    since_time=$(_epoch_to_iso_utc $((t0_epoch - 10)))
    if [ -n "$since_time" ]; then
        log_flag="--since-time=$since_time"
    else
        log_flag="--since=20m"
    fi
    echo "[chaos] Esperando 'Chaos metrics recorded' para '$alertname' (max ${budget}s)..." >&2
    while [ "$waited" -lt "$budget" ]; do
        local result
        result=$(kubectl logs -n "$NS_AGENT" "$agent_pod" $log_flag 2>/dev/null \
            | python3 -c "
import sys, json
for line in sys.stdin:
    line=line.strip()
    if not line:
        continue
    try:
        d=json.loads(line)
        if d.get('message')=='Chaos metrics recorded' and d.get('experiment','')==('$alertname'):
            print('{mttd}|{mttr}|{outcome}'.format(
                mttd=d.get('mttd_s','N/A'), mttr=d.get('mttr_s','N/A'), outcome=d.get('outcome','N/A')
            ))
    except Exception:
        pass
" 2>/dev/null | head -1 || true)
        if [ -n "$result" ]; then
            echo "[chaos] Métricas chaos capturadas: $result" >&2
            echo "$result"
            return 0
        fi
        sleep "$POLL_INTERVAL"
        waited=$((waited + POLL_INTERVAL))
    done
    echo "[chaos] WARN: 'Chaos metrics recorded' no encontrado en ${budget}s — is_chaos no disparó o prometheus fix pendiente" >&2
    return 1
}

_run_experiment() {
    local exp_type="$1" manifest="$2" deploy_label="$3" alertname="$4" pod_pattern="$5"

    echo ""
    echo "========================================================"
    echo " Chaos Experiment: $exp_type"
    echo "========================================================"

    echo "[chaos] Validando manifest (dry-run)..."
    kubectl apply -f "$manifest" --dry-run=client
    echo "[chaos] dry-run OK"

    echo "[chaos] Aplicando manifest: $manifest"
    kubectl apply -f "$manifest"
    local T0
    T0=$(date +%s)
    echo "[chaos] T0 (apply) = $(date -d @"$T0" --iso-8601=seconds 2>/dev/null || date -r "$T0") ($T0)"

    local T_fail
    T_fail=$(_wait_for_pod_failure "$deploy_label" "$pod_pattern") || {
        echo "[chaos] Abortando — pod no entró en estado de fallo"
        kubectl delete deployment -n "$NS_CHAOS" "$deploy_label" --ignore-not-found
        return 1
    }
    local elapsed_fail=$((T_fail - T0))
    echo "[chaos] T_pod_fail: ${elapsed_fail}s desde T0"

    local T_agent_ts
    T_agent_ts=$(_wait_for_agent_log "$alertname" "$T0") || {
        echo "[chaos] Alerta no detectada en logs del agente — revisar Prometheus/Alertmanager"
        kubectl delete deployment -n "$NS_CHAOS" "$deploy_label" --ignore-not-found
        return 1
    }

    local T_DETECT="N/A"
    if [ -n "$T_agent_ts" ]; then
        local epoch_agent
        epoch_agent=$(_iso_to_epoch "$T_agent_ts")
        if [ "$epoch_agent" -gt 0 ]; then
            T_DETECT=$((epoch_agent - T0))
        fi
    fi

    # Capturar métricas autoritativas del agente (no-fatal: si falla, continúa con N/A)
    local chaos_mttd_s="N/A" chaos_mttr_s="N/A" chaos_outcome="N/A"
    local chaos_raw
    if chaos_raw=$(_wait_for_chaos_metrics "$alertname" "$T0"); then
        chaos_mttd_s=$(echo "$chaos_raw" | cut -d'|' -f1)
        chaos_mttr_s=$(echo "$chaos_raw" | cut -d'|' -f2)
        chaos_outcome=$(echo "$chaos_raw" | cut -d'|' -f3)
    fi

    local t0_iso
    t0_iso=$(_epoch_to_iso_utc "$T0")

    echo ""
    echo "-------- TABLA DE RESULTADOS --------"
    printf "Experimento     : %s\n" "$exp_type"
    printf "T0 (apply)      : %s\n" "${t0_iso:-$T0}"
    printf "T_pod_fail      : %ss desde T0\n" "$elapsed_fail"
    printf "T_detect_total  : %ss desde T0 (apply→primer log agente; contexto SRE)\n" "$T_DETECT"
    printf "MTTD (pipeline) : %ss (aiops_chaos_mttd_s; startsAt[firing]→webhook; latencia pura de pipeline)\n" "$chaos_mttd_s"
    printf "MTTR (pipeline) : %ss (aiops_chaos_mttr_s; startsAt→pipeline completo = MTTD + tiempo LLM)\n" "$chaos_mttr_s"
    printf "Outcome agente  : %s\n" "$chaos_outcome"
    echo "NOTA: for: OOMKilled=0m, CrashLoop/CPU=5m, BadImage=1m (el for: aparece en T_detect_total, no en MTTD)"
    if [ "$chaos_mttd_s" = "N/A" ]; then
        echo "NOTA: MTTD/MTTR N/A → is_chaos no disparó; verificar que prometheus fix está aplicado en cluster"
    fi
    echo "-------------------------------------"
    echo ""
    echo "[chaos] Comprueba Mattermost #alerts y:"
    echo "  kubectl logs -n $NS_AGENT \$(kubectl get pods -n $NS_AGENT -l app=agent -o name | head -1) --tail=50 | grep chaos"

    echo "[chaos] Limpiando deployment (namespace $NS_CHAOS se mantiene para inspección)..."
    kubectl delete deployment -n "$NS_CHAOS" "$deploy_label" --ignore-not-found
    echo "[chaos] Experimento $exp_type completado."
}

CMD="${1:-}"
case "$CMD" in
    oom)
        _run_experiment "OOMKilled" "$CHAOS_DIR/chaos-oom.yaml" "chaos-oom-target" "KubePodOOMKilled" "OOMKilled"
        ;;
    crashloop)
        _run_experiment "CrashLoopBackOff" "$CHAOS_DIR/chaos-crashloop.yaml" "chaos-crashloop-target" "KubePodCrashLoopBackOff" "CrashLoopBackOff|Error"
        ;;
    bad-image)
        _run_experiment "BadImage" "$CHAOS_DIR/chaos-bad-image.yaml" "chaos-bad-image-target" "KubePodImagePullBackOff" "ImagePullBackOff|ErrImagePull"
        ;;
    cpu)
        _run_experiment "HighCPU" "$CHAOS_DIR/chaos-cpu-stress.yaml" "chaos-cpu-target" "HighCPU" "Running"
        ;;
    status)
        echo "=== Pods en $NS_CHAOS ==="
        kubectl get pods -n "$NS_CHAOS" -o wide 2>/dev/null || echo "(namespace no existe o vacío)"
        echo ""
        echo "=== Deployments en $NS_CHAOS ==="
        kubectl get deployments -n "$NS_CHAOS" 2>/dev/null || echo "(namespace no existe o vacío)"
        ;;
    cleanup)
        echo "[chaos] Borrando namespace $NS_CHAOS..."
        kubectl delete namespace "$NS_CHAOS" --ignore-not-found
        echo "[chaos] Cleanup completado."
        ;;
    *)
        _usage
        ;;
esac
