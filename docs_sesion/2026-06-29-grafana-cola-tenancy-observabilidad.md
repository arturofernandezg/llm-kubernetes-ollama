---
fecha: 2026-06-29
slug: grafana-cola-tenancy-observabilidad
promoted: true
---

> Quinta sesión del 2026-06-29 (tras `f2-retirar-legacy-ingesta`). Arrancada con `/start`. Dos frentes encadenados: (1) cerrar el "Paso F" de F2 — paneles Grafana de la cola; (2) un hallazgo que salió al mirar el dashboard — el cluster pasó de mono-usuario a **compartido** (compañera) y la observabilidad no tenía frontera de tenant. Código/manifiestos yo; build/apply/kubectl Jay.

## Objetivo
- **Paso F (Gate 8)**: añadir paneles Grafana para las métricas `aiops_queue_*` que F2 ya emite pero no se visualizaban.
- (Emergente) **Tenancy de la observabilidad en cluster compartido**: al ver tiles DOWN en el panel "Scrape targets", analizar por qué y acotar reglas + panel a lo nuestro (`arturo-.*`).
- (Emergente) **HighCPU sobre Redis**: investigar la alerta firing que apareció en el diagnóstico.

## Hecho
- **Conteo de suite sellado**: `pytest -q` → **419 passed** (5 warnings preexistentes ajenos). El número en docs (07, CLAUDE.md) ya era correcto → sin cambios.
- **`k8s/grafana.yaml` — paneles cola (dashboard *AIOps Agent — Overview*)**:
  - Nueva fila "Cola Redis Streams (F2)" (row id 14) al final del array `panels` (`y=34`), 3 paneles `w:8`:
    - **Enqueued vs Processed / s** (id 15, timeseries): `rate(aiops_queue_enqueued_total[5m])` + `rate(aiops_queue_processed_total[5m])` con `legendFormat: "processed {{outcome}}"`.
    - **Queue depth (pending/unacked)** (id 16, timeseries): `aiops_queue_depth`.
    - **Durabilidad (rango)** (id 17, stat, `colorMode: background`): `increase(aiops_queue_reclaimed_total[$__range])` + `increase(aiops_queue_dead_total[$__range])`; override por nombre `dead-lettered` → rojo si >0.
  - Comentario-índice del ConfigMap 4→5 filas; `version` 1→2.
  - Aplicado + `rollout restart deploy/grafana` (svc real = **`grafana-svc`**, port 3000). **Verificado en screenshots** (`demo/grafana_overview_top.png`, `demo/grafana_queue_row.png`): la fila renderiza, leyendas correctas, `depth` drena a 0 tras ráfaga, `reclaimed`/`dead-lettered` = 0 verde.
- **`k8s/prometheus.yaml` — tenancy de las 6 reglas**:
  - Añadido `namespace=~"arturo-.*"` a las 5 reglas basadas en KSM/cadvisor (OOM, CrashLoop, HighMemory ×2 lados del join, HighCPU ×2, ImagePullBackOff).
  - `TargetDown`: `up == 0` → **`up{job="kubernetes-endpoints"} == 0`** (excluye cadvisor a propósito).
  - Validado con `agent/.venv/bin/python` (YAML embebido OK, 6 alertas). `promtool` no instalado.
  - **Aplicado y verificado LIVE**: `curl .../api/v1/rules` muestra los 6 `query` con el filtro; `kubectl diff` vacío (cluster en sync).
- **`k8s/grafana.yaml` — panel "Scrape targets"** (Prioridad 3): `up` → `up{job="kubernetes-endpoints"}`, leyenda `{{job}}`→`{{service}}`, título → "Scrape targets (servicios propios)". Comentario-índice fila 3 actualizado; `version` 2→3.
- **`k8s/redis.yaml` — bump CPU**: `request 10m→50m`, `limit 50m→150m` (memoria intacta 128Mi). Pendiente de aplicar por Jay (ver gotcha de re-ensure del grupo).
- **`k8s/prometheus.yaml` — dejar de INGERIR métricas ajenas (Prioridad 2, finalmente sí)**. Jay pidió leer solo lo suyo, no solo alertar sobre lo suyo. Dos fuentes, dos cortes:
  - **KSM en el origen**: `--namespaces=arturo-llm-test,arturo-monitoring,arturo-mattermost,arturo-chaos` en el Deployment de kube-state-metrics → deja de *generar* `kube_*` de namespaces ajenos. Corte total (ni se fetchean).
  - **cadvisor en ingestión**: `metric_relabel_configs` con `keep` sobre `namespace=~"arturo-.*"` en el job → descarta `container_*` ajenos antes del TSDB. Honesto: el scrape al nodo SIGUE ocurriendo (no se puede filtrar el SD `role: node` por namespace); esto descarta en ingestión, no evita el fetch.
  - Validado con `agent/.venv/bin/python`: endpoints scoped, cadvisor con metric_relabel, KSM con args. **Aplicado y verificado LIVE**: tras `rollout restart` de KSM+Prometheus → `kube_pod_info{brms-gorules}=0` (prueba directa contra KSM), y `count by (namespace) (container_memory_working_set_bytes)` devuelve solo `arturo-*`. Residuo inofensivo: `kube_namespace_*` (objeto Namespace cluster-scoped, no filtrable por `--namespaces`, no usado por reglas).
  - **Gotcha de verificación**: justo tras el `rollout restart`, los `count by namespace` siguen mostrando namespaces ajenos por la **ventana de staleness de 5 min** de Prometheus (series pre-restart aún "frescas"). No es fallo — esperar 5 min o probar directo contra el endpoint de KSM (`port-forward` + `grep kube_pod_info`). Confirmar contra la fuente, no contra el instant query inmediato.

## Encontrado / gotchas
- **La fuga de tenancy confirmada con datos.** `count by (namespace) (kube_pod_info)` devolvió: `arturo-*` (los 4 míos) **+ `brms-gorules`** (compañera: BRMS/GoRules) + `kube-system` (29) + `cnrm-system` + `configconnector-operator-system`. KSM tiene `ClusterRole` cluster-wide → veía TODO. Antes del fix, un OOM/CrashLoop/ImagePull en cualquiera de esos namespaces ajenos disparaba alertas en MI Alertmanager → webhook → cola → LLM → Mattermost. La remediación estaba protegida (RBAC del agente es `Role` namespaced a `arturo-llm-test`/`arturo-chaos`, no puede tocar pods ajenos) pero el **ruido + ciclos de LLM + escalaciones** sí ocurrían.
- **Los dos jobs de scrape se comportan OPUESTO ante cluster compartido**:
  - `kubernetes-cadvisor` (`role: node`, sin filtro) = **cluster-wide** → descubre TODOS los nodos (incl. los que corren workloads de ella); ingesta sus métricas de contenedor; sus nodos NotReady/spot-preemptados salen DOWN en mi panel.
  - `kubernetes-endpoints` (`namespaces: [arturo-llm-test, arturo-monitoring]`) = **namespace-scoped** → aislado de ella. Un endpoints DOWN es de MI stack.
- **Los tiles DOWN del screenshot eran TRANSITORIOS.** `up == 0` salió **vacío** en el diagnóstico → nada caído en ese instante. Los DOWN del panel fueron residuo de los `rollout restart` de hoy (agent/grafana, lado endpoints) + rotación de nodos spot (lado cadvisor), ya recuperados.
- **`kubectl apply` → `configmap ... unchanged` asustó pero era benigno.** Jay ya había aplicado en un paso intermedio; el segundo apply no veía diff. Confirmado que el código estaba en disco (grep) + el estado vivo correcto (rules API + `kubectl diff` vacío). Lección: ante `unchanged` inesperado, **verificar estado vivo** (`kubectl diff` / API), no asumir.
- **HighCPU sobre Redis — diagnóstico CORREGIDO al final de la sesión (era busy-spin, no carga ligera).** Primero concluí: el `consume_loop` usa `XREADGROUP COUNT 1 BLOCK 5000` (consumer educado) → el limit de 50m simplemente quedó corto. El `rate(cpu redis[5m])` dio 0.0347≈35m sobre 50m = 70% basal. **PERO** al desplegar el bump de Redis, los logs del pod VIEJO revelaron la causa real: tras recrear Redis (el bump recreó el pod → stream + grupo desaparecidos), el pod viejo del agente entró en **busy-spin de `XREADGROUP failed: NOGROUP`** — decenas de errores en ~50ms (cientos/s) martilleando Redis. Un consumer en BLOCK 5000 nunca llega a 45m; un busy-spin NOGROUP **sí satura los 50m** → ESO disparó el HighCPU (probablemente durante las pruebas de Slice 4, que borraban el stream con `DEL`+restarts → ventanas con pod vivo sin grupo). El bump a 150m da headroom pero **NO cura la raíz**: si entra en spin, quema 150m igual.
- **BUG real en `consume_loop` (self-healing roto)**: al capturar el error de XREADGROUP, el `except` loguea y hace `continue` **inmediato** — sin el block de 5s (que solo aplica en éxito) y **sin recrear el grupo**. Consecuencia: si Redis se recrea/flushea bajo un agente vivo, el consumer gira para siempre (solo se hace `ensure_group` en el lifespan de arranque). Justo lo contrario de la durabilidad que F2 promete. El pod NUEVO arranca bien (ensure_group al inicio contra Redis fresco → readyz 200, sin NOGROUP), así que solo muerde si Redis cae SIN reiniciar el agente.
- **El panel "Salud del Sistema" NO se arregla con el fix de reglas.** Reglas = qué alerta; panel = qué dibuja (query `up` en crudo). Son fuentes distintas → hubo que tocar AMBOS (`prometheus.yaml` para alerting, `grafana.yaml` para el panel).
- **Gotcha de deploy de Redis efímero**: sin PVC → recrear el pod (bump de recursos) pierde stream/PEL/escalaciones, y el consumer group desaparece. El `consume_loop` NO regenera el grupo (solo se crea en lifespan startup) → tras recrear Redis hay que **reiniciar el agente** para re-`ensure_group`. Secuencia: `apply redis && rollout status redis && rollout restart agent`. Inocuo ahora (cola drenada).
- **Servicios reales**: `grafana-svc` (3000), `prometheus-svc` (9090), `agent-svc` (8000, no 80). El PF muere con cada rollout → patrón `pkill -f "port-forward.*X"; sleep 2; kubectl port-forward ... & sleep 3`.

## Decisiones + por qué
- **Acotar las 6 reglas a `arturo-.*` (no por job ni por relabel global).** Lo de la compañera es su responsabilidad; yo quiero alertar/demostrar solo lo mío. Filtro en el `expr` = quirúrgico, sin tocar la config de scrape (que sí ingiere métricas ajenas pero eso es coste/cardinalidad, no alerting). Prioridad 1.
- **`TargetDown` → solo `job=kubernetes-endpoints`, excluyendo cadvisor.** cadvisor es cluster-wide e incluye rotación de nodos spot ajenos → `severity: critical` falsos. La caída de un pod MÍO se sigue cubriendo vía su endpoint en ese job. La salud de nodos no es responsabilidad de un tenant.
- **Panel "Scrape targets" → `up{job="kubernetes-endpoints"}` + leyenda `{{service}}`.** Decisión explícita de Jay ("quiero enseñar lo mío"). Trade-off aceptado: se pierde visibilidad de nodos/cadvisor en ESE panel (correcto para un tenant). Antes la leyenda `{{job}}` daba un muro de "kubernetes-cadvisor" indistinguibles (el relabel reescribe `__address__` al proxy → `instance` idéntico en todos).
- **Prioridad 2 (dejar de ingerir lo ajeno): hecha, pero con el enfoque correcto.** Descartado el `keep` por label de NODO (frágil: comparto node-pools con ella, mis pods spread por nodos compartidos). En su lugar: KSM `--namespaces` (corte en origen, limpio) + cadvisor `metric_relabel keep namespace=~"arturo-.*"` (corte en ingestión, robusto sea cual sea el nodo). Trade-off honesto asumido: cadvisor `role: node` obliga a hacer el HTTP scrape a cada nodo igualmente (incl. nodos solo-suyos); el `metric_relabel` evita el ALMACENAMIENTO de lo ajeno, no el fetch. Para "solo lo mío en mi TSDB/dashboards/cardinalidad" es la respuesta completa; evitar el fetch requeriría filtrar el SD por nodo (frágil) y no compensa.
- **Las métricas cluster-scoped de KSM (kube_node_*, PVs) siguen cluster-wide** aun con `--namespaces` (el flag solo acota recursos namespaced). No las usamos en reglas → inocuo; no merece `--metric-denylist`.
- **Bump CPU Redis 50m→150m**: se mantiene como **headroom** (bajo riesgo, coherente con el bump de memoria de F2, `request 10m→50m` para reserva realista) — PERO al final de la sesión se descubrió que NO es el fix de la causa raíz (ver gotcha NOGROUP). La raíz se arregla en código (`streams.py`), no subiendo el límite. El bump queda como defensa en profundidad.

## Siguiente
- **Aplicar el bump de Redis** (Jay): `kubectl apply -f k8s/redis.yaml -n arturo-llm-test && kubectl rollout status deploy/redis -n arturo-llm-test && kubectl rollout restart deploy/agent -n arturo-llm-test`. Verificar logs `Redis connected`→`Consumer group created` + `/readyz=200`. Tras unos min sin tráfico, re-correr `rate(cpu redis[5m])` → debe caer de 35m y HighCPU dejar de firing.
- **Confirmar el panel "Scrape targets" live** (visual en Grafana: solo `agent-svc` + `kube-state-metrics` en UP). El apply de grafana decía `unchanged` — verificar con `kubectl diff -f k8s/grafana.yaml -n arturo-monitoring` o a ojo.
- **Commits** (Jay, sin Co-Authored-By), sugeridos separados por tema:
  - `feat: paneles Grafana aiops_queue_* (F2, Gate 8)` → `k8s/grafana.yaml` (fila cola) + `demo/grafana_*.png`.
  - `fix: acotar observabilidad a namespaces arturo-* (tenancy en cluster compartido)` → `k8s/prometheus.yaml` (6 reglas) + `k8s/grafana.yaml` (panel scrape targets).
  - `fix: subir CPU limit de Redis 50m→150m (carga de cola F2)` → `k8s/redis.yaml`.
  - `fix: KSM + cadvisor solo ingieren namespaces arturo-* (dejar de leer workloads ajenos)` → `k8s/prometheus.yaml`.
- **Aplicar el corte de ingestión** (Jay): `kubectl apply -f k8s/prometheus.yaml -n arturo-monitoring && kubectl rollout restart deploy/kube-state-metrics deploy/prometheus -n arturo-monitoring`. **Verificar que ya solo se ve lo mío**: `count by (namespace) (kube_pod_info)` y `count by (namespace) (container_memory_working_set_bytes)` deben devolver SOLO `arturo-*` (sin `brms-gorules`/`kube-system`/`cnrm-system`).
- **`/promote`** cuando se consolide: el hallazgo de tenancy es oro para `docs/14` (production-readiness: "qué pasa con tu observabilidad cuando dejas de ser único tenant"); paneles cola → `docs/02` + nota "Paso F hecho" en `docs/07`; bump Redis → CLAUDE.md (k8s/redis.yaml) + modos de fallo.
- **Screenshot de Gate 8 en vivo**: lanzar ráfaga (`burst-*`) justo antes de capturar para que `enqueued`/`processed` muestren caudal, no "Last: 0 req/s".
- **★ PRIORITARIO próxima sesión — `streams.py` self-healing ante NOGROUP** (causa raíz del HighCPU de Redis): en `consume_loop`, capturar el `ResponseError` de NOGROUP de forma específica → llamar `ensure_group` (recrear grupo con MKSTREAM) + backoff antes de reintentar, en vez del `continue` inmediato. Hace el consumer self-healing si Redis se recrea bajo un agente vivo y mata el busy-spin. Cambio quirúrgico (un `except` específico) + test (NOGROUP → ensure_group llamado, sin busy-loop). Es el verdadero fix de F2-durabilidad que el bump de Redis solo parcheó.
- Pendiente de siempre (cluster): matriz E1–E6 (`docs/14`); F3 (HPA/CPU).
