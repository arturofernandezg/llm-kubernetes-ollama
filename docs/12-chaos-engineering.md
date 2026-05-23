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

**Nota**: MTTD y MTTR usan `alert.startsAt` (timestamp cuando Alertmanager entró en `firing`) como T0. Este T0 incluye el periodo `for:` de la regla Prometheus. El MTTD mínimo observable es igual al `for:` period de cada regla.

Consultar métricas en cluster:
```
kubectl exec -n arturo-llm-test <agent-pod> -- curl -s localhost:8000/metrics | grep aiops_chaos
```

## Tabla de resultados

| Fecha | Experimento | `for:` | T_pod_fail | MTTD (s) | MTTR (s) | confidence | outcome | Notas |
|---|---|---|---|---|---|---|---|---|
| - | OOMKilled | 0m | - | - | - | - | - | Pendiente ejecución en cluster |
| - | CrashLoopBackOff | 5m | - | - | - | - | - | Pendiente ejecución en cluster |
| - | ImagePullBackOff | 1m | - | - | - | - | - | Pendiente ejecución en cluster |
| - | HighCPU | 5m | - | - | - | - | - | Pendiente ejecución en cluster |

## Cómo interpretar los resultados

- **MTTD < 120s (OOM)**: el pipeline de detección es ágil. Con `for: 0m`, el objetivo es MTTD < 90s.
- **MTTD < 400s (CrashLoop / HighCPU)**: el `for: 5m` introduce 300s de latencia mínima. MTTD objetivo < 360s.
- **MTTD < 120s (ImagePullBackOff)**: `for: 1m` introduce 60s de latencia mínima. MTTD objetivo < 90s.
- **MTTR = MTTD + tiempo_LLM**: el LLM (qwen2.5:1.5b) añade ~60-200s. MTTR objetivo < 300s (OOM/BadImage), < 600s (CrashLoop/HighCPU).
- **confidence > 0.7**: el RAG encontró runbooks relevantes y el LLM generó un diagnóstico estructurado.
- **outcome = escalate**: remediación bloqueada por regla 4.5 (pod restart) o 4.6 (>2x memory). Esperado con `DRY_RUN=true`.
- **BadImage no genera restarts**: `kube_pod_container_status_restarts_total` queda en 0; `KubePodCrashLoopBackOff` **no** dispara para este caso. Por eso se añadió la regla `KubePodImagePullBackOff`.

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
| Sesión #6 (Mini-Fase 4) | 2026-05-19 | Ejecución real en cluster — OOM, CrashLoop, BadImage, HighCPU | Tabla MTTD/MTTR completada con datos reales. Screenshot `docs/img/grafana-chaos.png`. |
