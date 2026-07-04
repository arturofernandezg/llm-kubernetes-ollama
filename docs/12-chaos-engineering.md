# Chaos Engineering — AIOps Infrastructure Agent

## Filosofía

Chaos Engineering controlado: se provocan fallos reales en un namespace aislado (`arturo-chaos`) para medir el comportamiento del agente bajo condiciones de fallo. El objetivo no es romper producción, sino obtener **números reales** (MTTD, MTTR, confidence) que respalden la defensa y guíen mejoras del sistema.

Los experimentos corren siempre con `REMEDIATION_DRY_RUN=true` hasta acuerdo explícito con el tutor.

## Namespace de aislamiento

```
arturo-chaos   # experimentos chaos
arturo-llm-test   # agente, ChromaDB, Ollama (producción)
arturo-monitoring # Prometheus, Alertmanager, Grafana
arturo-mattermost # Mattermost + PostgreSQL
```

El agente detecta alertas del namespace `arturo-chaos` e incrementa métricas específicas (`aiops_chaos_*`) sin alterar el flujo normal del pipeline.

## Manifests disponibles

| Manifest | Fallo simulado | Alerta disparada | `for:` |
|---|---|---|---|
| `k8s/chaos/chaos-oom.yaml` | OOMKilled (stress-ng > memory limit) | `KubePodOOMKilled` | 0m |
| `k8s/chaos/chaos-crashloop.yaml` | CrashLoopBackOff (`/bin/false`) | `KubePodCrashLoopBackOff` | 5m |
| `k8s/chaos/chaos-bad-image.yaml` | ImagePullBackOff (tag inexistente) | `KubePodImagePullBackOff` | 1m |
| `k8s/chaos/chaos-cpu-stress.yaml` | HighCPU (stress-ng --cpu 2 > limit 100m) | `HighCPU` | 5m |

> **Validación de auto-remediación real (slice 6, 2026-07-01)**: con `da7aafb` (re-sourcing) y `REMEDIATION_DRY_RUN=false`, `chaos.sh oom` dispara el auto de verdad — logs `Rule 5 bypassed: structured remediation` + comando `set resources ... --limits=memory=512Mi` bien sintetizado. **Pero destapó la causa raíz real**: el `name/namespace/container/current/new` del comando salen de `proposed_action` (LLM) y el 1.5b los **alucina** (target=namespace, container=pod, `current='256Mi, 512Mi, 1Gi'` fabricado) → `NotFound`/`unparseable`. El target es problema de **sourcing**, no de inteligencia → **slice C** lo sella desde los labels de la alerta + snapshot del cluster (ver `docs/07` §F3). Gotcha operativo: `chaos.sh cleanup` borra el ns `arturo-chaos` → se lleva el RBAC; recrear ns + `apply rbac` antes del experimento (ver `docs/03`).

## Prerrequisito: mirror de imágenes

`polinux/stress` no está en Artifact Registry. Mirrorear **antes** de aplicar `chaos-oom.yaml` o `chaos-cpu-stress.yaml`:

```
crane copy --platform linux/amd64 polinux/stress:latest europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/polinux-stress:latest
```

`busybox:stable` (usado en chaos-crashloop) ya debe estar disponible o puede mirrorear igual.

**`chaos-bad-image.yaml` NO requiere mirror**: la imagen `nonexistent:v999` debe fallar el pull — ese es el objetivo del experimento.

## Prerrequisito: regla Prometheus KubePodImagePullBackOff

La regla 6 (`KubePodImagePullBackOff`) se añadió en `k8s/prometheus.yaml` en la sesión #2. Al aplicar el manifest hay que recargar Prometheus (los ConfigMaps no recargan automáticamente):

```bash
kubectl apply -f k8s/prometheus.yaml && kubectl rollout restart deployment/prometheus -n arturo-monitoring
```

Verificar que la regla está activa:
```bash
kubectl port-forward svc/prometheus-svc 9090:9090 -n arturo-monitoring
# http://localhost:9090/rules → buscar KubePodImagePullBackOff
```

## Cómo ejecutar

```bash
# Dar permisos de ejecución (solo la primera vez)
chmod +x scripts/chaos.sh

# Experimento OOM (requiere mirror previo de polinux/stress)
./scripts/chaos.sh oom

# Experimento CrashLoopBackOff
./scripts/chaos.sh crashloop

# Experimento ImagePullBackOff (imagen inexistente, requiere regla KubePodImagePullBackOff activa)
./scripts/chaos.sh bad-image

# Experimento HighCPU (requiere mirror de polinux/stress; for: 5m → MTTD mínimo 300s)
./scripts/chaos.sh cpu

# Ver estado de pods en arturo-chaos
./scripts/chaos.sh status

# Limpiar todo el namespace arturo-chaos
./scripts/chaos.sh cleanup
```

El script hace dry-run antes del apply, registra T0, sondea pods y logs del agente, e imprime la tabla de resultados.

## Métricas Prometheus expuestas

El agente registra las siguientes métricas cuando `namespace == arturo-chaos`:

| Métrica | Tipo | Labels | Descripción |
|---|---|---|---|
| `aiops_chaos_experiment_total` | Counter | `experiment`, `outcome` | Experimentos procesados |
| `aiops_chaos_mttd_seconds` | Histogram | `experiment` | MTTD: desde `startsAt` hasta recepción webhook |
| `aiops_chaos_mttr_seconds` | Histogram | `experiment` | MTTR: desde `startsAt` hasta pipeline completo |

**Nota (corregido con datos reales 2026-05-26)**: MTTD y MTTR usan `alert.startsAt` como T0. Empíricamente, el `startsAt` que recibe el agente corresponde a la **transición a `firing`** (es decir, *después* de que el periodo `for:` ya transcurrió), **no** a `ActiveAt`. Por tanto MTTD/MTTR miden **latencia pura del pipeline** (firing → webhook → diagnóstico) y **NO incluyen el periodo `for:`**. Evidencia: con `for:` distintos, el MTTD se mantuvo en 5–10s (BadImage `for:1m` → 5.1s, HighCPU `for:5m` → 10s); si `startsAt` fuese `ActiveAt`, esos MTTD serían ≥60s y ≥300s respectivamente. El periodo `for:` aparece en **`T_detect_total`** (apply → primer log), no en MTTD.

Consultar métricas en cluster:
```
kubectl exec -n arturo-llm-test <agent-pod> -- curl -s localhost:8000/metrics | grep aiops_chaos
```

## Hipótesis y criterios de éxito

Estructura Principles of Chaos: se define el estado estable, se formula una hipótesis, se inyecta el fallo y se mide si el sistema cumple los criterios de éxito. Si no cumple, se aborta con `bash scripts/chaos.sh cleanup`.

**Blast radius compartido (todos los experimentos):** confinado a namespace `arturo-chaos` + nodos `guaranteed=true` + resource limits por container + gates de seguridad multicapa del motor de remediación (rule 4.5 → escalate típicamente; DRY_RUN=false mitigado por rules 4.5/4.6/5).

Criterio de MTTD común a todos: dado que MTTD(pipeline) mide latencia pura (firing→webhook, independiente de `for:`), el umbral es **MTTD(pipeline) < 30s**. El periodo `for:` se valida vía `T_detect_total` (apply→detección), con cota aproximada por experimento.

| Experimento | Steady state | Hipótesis | Criterio de éxito | Abort |
|---|---|---|---|---|
| OOMKilled | Agente Running/Ready, 0 alertas chaos activas, Mattermost silencioso | El agente detecta el OOMKill y notifica en Mattermost con el pod correcto (`NS: arturo-chaos`), sin afectar el pipeline principal | MTTD(pipeline) < 30s; T_detect_total < 60s (for:0m); Mattermost muestra `chaos-oom-target-xxx`; is_chaos disparó | `bash scripts/chaos.sh cleanup` |
| CrashLoopBackOff | Idem | El agente detecta el crashloop acumulado (>3 reinicios/15m) y escala/notifica con el pod correcto | MTTD(pipeline) < 30s; T_detect_total ~for:5m + acumulación de reinicios; outcome ∈ {escalate, auto_remediate} | `bash scripts/chaos.sh cleanup` |
| ImagePullBackOff | Idem | El agente detecta el pull-fail crónico y notifica con el pod correcto | MTTD(pipeline) < 30s; T_detect_total < 240s (for:1m + backoff de pull); Mattermost muestra `chaos-bad-image-target-xxx` | `bash scripts/chaos.sh cleanup` |
| HighCPU | Idem | El agente detecta CPU saturada con join cAdvisor/KSM correcto (fix prometheus aplicado) y notifica | MTTD(pipeline) < 30s; T_detect_total ~for:5m + rate[5m] ramp (~660s); Mattermost muestra el pod correcto en `arturo-chaos` | `bash scripts/chaos.sh cleanup` |

## Tabla de resultados

**Definición de métricas:**
- **MTTD (pipeline)** = `aiops_chaos_mttd_seconds` = `alert.startsAt → recepción webhook`. `startsAt` ≈ transición a `firing` (post-`for:`), por lo que mide **latencia pura del pipeline y NO incluye el `for:`** (verificado empíricamente, ver Nota arriba). Métrica autoritativa, registrada en Prometheus y visible en Grafana.
- **MTTR (pipeline)** = `aiops_chaos_mttr_seconds` = `alert.startsAt → pipeline completo` (LLM + Mattermost). = MTTD + tiempo_LLM. Dominado por la inferencia del LLM en CPU (~205–270s con qwen2.5:1.5b).
- **T_detect_total** = `T0_apply → primer log agente`. Mayor que MTTD: incluye scheduling del pod + el periodo `for:` + ramp de la métrica hasta que la condición es true. Es donde se observa el efecto del `for:`. Contexto SRE.

| Fecha | Experimento | `for:` | T_pod_fail | MTTD pipeline (s) | MTTR pipeline (s) | confidence | outcome | Notas (T_detect_total) |
|---|---|---|---|---|---|---|---|---|
| 2026-05-27 | OOMKilled | 0m | 12s | **5.0** | **205.4** | 0.95 | escalate | T_detect_total 40s. RAG limpio post-fix: cita pod correcto (`chaos-oom-target … arturo-chaos`) |
| 2026-05-27 | CrashLoopBackOff | 5m | 6s | **5.0** | **205.7** | 0.95 | escalate | T_detect_total 46s |
| 2026-05-27 | ImagePullBackOff | 1m | 6s | **5.1** | **252.1** | 0.80 | escalate | T_detect_total 173s |
| 2026-05-27 | HighCPU | 5m | 5s | **10.1** | **206.7** | 0.00 | suggest_only | T_detect_total 631s (for:5m + rate[5m] ramp). LLM no propuso comandos concretos para CPU overuse (confidence=0, commands=0) |

**Metodología de verificación (2026-05-27):** Todos los valores provienen del log `"Chaos metrics recorded"` en `agent/main.py` (bloque `if is_chaos:`), con `is_chaos = namespace == "arturo-chaos"`. Prerrequisitos: (1) fix `metric_relabel_configs` aplicado en Prometheus (fix label collision KSM/cAdvisor, applied 2026-05-26); (2) RAG limpiado de 92 incidents contaminados (namespace=arturo-monitoring / pod=kube-state-metrics). Ambos verificados antes del re-run.

## Cómo interpretar los resultados

- **MTTD(pipeline) = 5–10s en todos los experimentos**: independiente del `for:` (mide firing→webhook, latencia pura). Los 4 cumplen el umbral < 30s. El `for:` se observa en `T_detect_total`, no aquí.
- **T_detect_total refleja el `for:` + ramp + scheduling**: OOM 39s (`for:0m`), BadImage 171s (`for:1m` + backoff de pull), CrashLoop 195s, HighCPU 609s (`for:5m` + `rate[5m]` ramp). Es el número que un SRE percibe como "tiempo hasta que saltó la alerta".
- **MTTR = MTTD + tiempo_LLM**: dominado por la inferencia del LLM (qwen2.5:1.5b en CPU), ~205–255s. Medidos: 205.4–252.1s. Todos por debajo de `HTTP_TIMEOUT=360s` con margen.
- **confidence varía por tipo de fallo**: OOM/Crashloop (0.95) — escenarios bien cubiertos por runbooks; BadImage (0.80) — pull-fail menos frecuente; HighCPU (0.00) — el LLM no pudo estructurar comandos de remediación (CPU overuse no tiene un fix kubectl directo análogo a bump de memoria). **Caveat de honestidad**: los campos *estructurados* (pod/NS/alertname) provienen del header de la alerta (siempre correctos); el *razonamiento free-text* del modelo pequeño puede ser impreciso. La confianza alta no implica razonamiento correcto.
- **outcome**: 3/4 `escalate` (OOM/Crashloop/BadImage) — remediación bloqueada por regla 4.5/4.6/5 (safety gates). 1/4 `suggest_only` (HighCPU, confidence=0.00, commands=0) — el motor de decisión no tenía comandos que ejecutar. Todos los outcomes son correctos y esperados.
- **BadImage no genera restarts**: `kube_pod_container_status_restarts_total` queda en 0; `KubePodCrashLoopBackOff` **no** dispara para este caso. Por eso se añadió la regla `KubePodImagePullBackOff`.

## Validación del arco completo v2 (2026-07-04, `8a40fdc`)

La tabla de arriba mide **detección + notificación** (MTTD/MTTR del pipeline). El 2026-07-04 se corrió por primera vez en cluster el **arco completo de remediación v2** (chaos OOM sobre `chaos-oom-target`): detección → grounding (enrichment) → diagnóstico → seal → cap 4.6 → escalación → **approve humano** → patch → ventana rollback → veredicto → re-upsert R2. No es un experimento de latencia sino de **corrección del arco de remediación**.

**Qué se validó (todo verde):**
- **Grounding Eje A real**: el snapshot trae `last_state_reason=OOMKilled`, `workload_kind=Deployment` por ownerReferences con pod vivo; `seal_proposed_action` saca `current_value=32Mi` **del cluster** (no del LLM); `ground_confidence: grounded=1.0, model=0.95`. La clase de fallo `NotFound`/alucinación del slice 6 está **muerta** en el camino grounded.
- **Safety cap 4.6**: el LLM propuso `512Mi` (16× el límite de 32Mi) → `blocked: exceeds 2x current` → **escala** (no clampa en silencio, acuerdo tutor "overshoot >2× escala").
- **Human-in-the-loop E2E**: escala → approve (HMAC OK) → `kubectl set resources … 512Mi` → persistió. Botón ✅ validado.
- **Rollback durable (P0·3) + R2**: el approve programó rollback (paridad humano/auto), +300s → eval → veredicto re-upsertado en ChromaDB.

**Aprendizaje clave — falso rollback por restart benigno:** tras aprobar 512Mi, el health-check dio `healthy=false, reason="pods_restarting: [3]"` y revirtió a 32Mi (`outcome=reverted`). Pero el pod **NO OOMeaba** a 512Mi: los 3 restarts eran del ciclo `stress --timeout 60` (stress sale con exit 0 cada 60s → el contenedor reinicia por término limpio). **El health-check cuenta restarts sin mirar el motivo** → interpretó exit-limpio como crash. Es un **artefacto del manifiesto**, no del sistema: en un OOM real, fix que cura = 0 restarts = healthy; fix que falla = sigue OOMeando = revert (el heurístico es correcto para OOM real). **Fix aplicado**: `k8s/chaos/chaos-oom.yaml` sin `--timeout` (stress infinito → restarts solo significan OOM). **Mejora de producto pendiente (v2.x, C-01 en docs/11)**: el health-check debería mirar `lastState.reason==OOMKilled`, no solo contar restarts.

**Limitación del harness — `scripts/chaos.sh` no observa el arco:** su ciclo de auto-cleanup (~300s) es **más corto que el arco completo** (patch → 300s ventana rollback → veredicto ≈10min) → borra el deployment a mitad → `NotFound` en captura/remediación/rollback, y sin pod vivo el enrichment cae a `skipped` → seal sin grounding. **Para validar el arco: aplicar el manifiesto a mano** (`kubectl apply`); `scripts/chaos.sh` sirve solo para medir MTTD/MTTR de detección (C-04 en docs/11).

**MTTR = techo de hardware:** el LLM (qwen2.5:1.5b en CPU, todo el nodo e2-standard-2 sin GPU) tarda 147-213s warm por diagnóstico y timeoutea a 360s en cold. MTTD sigue en 5s; el MTTR está dominado por la inferencia. No bajable con más CPU (C-05 en docs/11).

**Pendiente**: el veredicto `cured` positivo (solo se tiene `rolled_back`). Con el manifiesto arreglado, el run limpio de S3·b lo cerrará → habilita R4 (gráfica `aiops_feedback_verdict_total` con `cured`+`rolled_back`).

## Visualización en Grafana

El dashboard **"AIOps — Chaos"** provisionado en `k8s/grafana.yaml` (ConfigMap
`grafana-dashboard-aiops`, key `chaos.json`) visualiza las métricas
`aiops_chaos_*` en tiempo real.

### Paneles

| Panel | Tipo | Métrica | Notas |
|---|---|---|---|
| MTTD p95 por experimento | timeseries | `aiops_chaos_mttd_seconds_bucket` | Threshold: verde <90s / amarillo <360s / rojo ≥360s |
| MTTR p95 por experimento | timeseries | `aiops_chaos_mttr_seconds_bucket` | Threshold: verde <300s / amarillo <600s / rojo ≥600s |
| MTTD p50 / p95 (global) | timeseries | `aiops_chaos_mttd_seconds_bucket` | Dos series, sin desglose por experimento |
| Experimentos por outcome | piechart | `aiops_chaos_experiment_total` | Distribución escalate / auto_remediate / blocked |
| Total experimentos | stat | `aiops_chaos_experiment_total` | Contador acumulado |
| ALERTS firing (arturo-chaos) | timeseries | `ALERTS{namespace="arturo-chaos"}` | Correlación alerta Prometheus ↔ agente |
| Experimentos última hora | table | `increase(aiops_chaos_experiment_total[1h])` | Por experiment + outcome |

**Nota**: los paneles de histograma muestran "No data" hasta que se ejecute
al menos un experimento chaos. Es esperado.

### Runbook de acceso

```bash
kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring
```

Abrir http://localhost:3000 → Login (admin / `kubectl get secret grafana-admin -n arturo-monitoring -o jsonpath='{.data.admin-password}' | base64 -d`) → Dashboards → **AIOps — Chaos**.

Para poblar series antes del screenshot:

```bash
./scripts/chaos.sh oom
```

Screenshot guardado en `docs/img/grafana-chaos.png` (capturado en S6 tras ejecución de experimentos).

## Sesiones completadas

| Sesión | Fecha | Experimentos | Estado |
|---|---|---|---|
| Sesión #1 (Mini-Fase 4) | 2026-05-18 | OOM + CrashLoopBackOff | Infraestructura lista, pendiente ejecución |
| Sesión #2 (Mini-Fase 4) | 2026-05-19 | ImagePullBackOff + HighCPU | Infraestructura lista, pendiente ejecución |
| Sesión #3 (Mini-Fase 4) | 2026-05-18 | Dashboard Grafana "AIOps — Chaos" | Dashboard provisionado en `k8s/grafana.yaml` |
| Sesión #6 (Mini-Fase 4) | 2026-05-19 | Hardening pre-prod (código): smoke.sh, chaos.sh fixes (C1/B2), prometheus label fix | Código listo; ejecución en cluster pendiente |
| Sesión pruebas E2E | 2026-05-26 | Gates 0-5 verificados; hallazgo crítico: is_chaos=false por label collision KSM/cAdvisor; fix metric_relabel_configs aplicado | ⚠️ Datos pre-fix sin verificar — re-run pendiente |
| Sesión re-run + FASE 2 | 2026-05-27 | RAG limpiado (92 incidents contaminados borrados); re-run 4/4 experimentos con is_chaos=true verificado; backup ChromaDB limpio | ✅ 4/4 datos reales verificados. 3/4 escalate, 1/4 suggest_only (HighCPU). Tabla de resultados actualizada con números autoritativos. |
