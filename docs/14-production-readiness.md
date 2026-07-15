# Production-Readiness — Validación F1

> **Entregable de la Fase F1** del roadmap (`docs/07`). Estado (**cierre honesto 2026-07-12, pre-chapter**): los 7 hallazgos PR-01..07 están **resueltos**; de la matriz E1–E6, **E4/E4b/E5 quedaron respondidos por la validación en cluster de F2** (2026-06-29) y el resto queda como **protocolo definido, no ejecutado** — el fail-open individual de cada dependencia está cubierto por tests mockeados, pero el veredicto en cluster no se reclama. Se cierra así a propósito antes del chapter en vez de correr experimentos con prisa.
> Regla de la casa: *docs reflect reality, not ambition*. Por eso los hallazgos de §2 se marcan como **hipótesis a verificar**, no como hechos confirmados, hasta ejecutarse en cluster.

Objetivo F1: (1) trazar el pipeline E2E hasta narrarlo de memoria, (2) provocar fallos en las dependencias propias (ChromaDB / Ollama / Redis) y demostrar fail-open, (3) test de concurrencia que evidencie el dedup de FASE 2 **y** cuantifique el cuello de botella que justifica F2, (4) salir con este informe honesto.

---

## 1. Protocolo de experimentos (matriz E1–E6)

Ejecutar con `kubectl` desde el Mac. Comandos en una sola línea (restricción Cloud Shell). Namespace de dependencias: `arturo-llm-test`. Antes de cada experimento, capturar el snapshot de métricas relevantes (`curl -s localhost:9090/metrics | grep aiops_`) para medir el delta.

| # | Experimento | Inyección (one-liner) | Fail-open esperado | Señal que lo evidencia | Veredicto |
|---|---|---|---|---|---|
| **E1** | ChromaDB caído | `kubectl scale statefulset chromadb -n arturo-llm-test --replicas=0` | Diagnóstico zero-shot (sin contexto RAG); el pipeline continúa | `aiops_diagnosis_total{outcome="rag_failed"}`↑ + `aiops_feedback_total{outcome="failed"}`↑ + log `RAG retrieval failed ... proceeding without context` + el mensaje a Mattermost llega igual | ⏳ pendiente |
| **E2** | Ollama caído (ConnectError) | `kubectl scale deploy ollama -n arturo-llm-test --replicas=0` | `diagnosis=None`, sin remediación; fallback a Mattermost **sin** nota de timeout | `aiops_diagnosis_total{outcome="llm_error"}`↑ — **ojo a PR-02 (readiness)**: ver nota abajo | ⏳ pendiente |
| **E2b** | Ollama lento (timeout real) | Inyectar latencia >`HTTP_TIMEOUT` (toxiproxy sidecar, o modelo grande temporal) | Mensaje diferenciado "LLM timeout — el modelo tardó más de Ns" | `aiops_diagnosis_total{outcome="llm_timeout"}`↑ + `_llm_timeout=True` → texto de Mattermost distinto al de E2 | ⏳ pendiente |
| **E3** | Redis caído (nueva escalación) | `kubectl scale deploy redis -n arturo-llm-test --replicas=0` + disparar alerta que escale | Mensaje a Mattermost **sin botones** + "_Botones interactivos no disponibles (Redis caído)_" | `aiops_escalation_store_total{outcome="redis_down"}`↑ (PR-06) + log `Redis unavailable — sending escalation without buttons` | ⏳ pendiente |
| **E3b** | Redis caído (click en escalación previa) | Igual que E3, con una escalación ya enviada y viva | `get_escalation`→None → "Escalación no encontrada o expirada" → no-op seguro | El botón no ejecuta nada; mensaje de expiración | ⏳ pendiente |
| **E4** | Dedup feliz | `for i in $(seq 1 10); do curl -s -XPOST localhost:8000/webhook/alert -H 'Content-Type: application/json' -d @burst_same.json & done; wait` (mismo alertname+ns+pod) | 1 pipeline corre, las otras 9 se saltan | `aiops_dedup_skipped_total{alertname}`=9 + 1 solo mensaje en Mattermost | ✅ **respondido por F2** (2026-06-29): dedup cluster-wide por fingerprint (SETNX) validado en cluster con la ráfaga de 10 (Slice 4) — la mecánica evolucionó (Redis, no in-memory) pero la garantía es la misma |
| **E4b** | Saturación (motivo de F2) | Mismo one-liner con N payloads **distintos** (`burst_distinct_*.json`) | N background tasks → N llamadas concurrentes a Ollama | Medir latencia p50/p99 y nº de `llm_failed` (timeouts) → **cuantificar el cuello que F2 elimina** | ✅ **respondido por F2** (2026-06-29): la cola serializa el consumo 1-a-1 — el webhook devuelve 200 inmediato y no hay N llamadas concurrentes a Ollama que medir; ráfaga de 10 sin pérdida validada en cluster (Slice 4). La pregunta original quedó obsoleta por diseño |
| **E5** | Restart mid-diagnóstico | Disparar alerta y, durante el LLM (~200s), `kubectl delete pod -n arturo-llm-test -l app=agent` | **Alerta PERDIDA**: las BackgroundTasks mueren con el proceso, el webhook ya devolvió 200 | Nunca llega el mensaje a Mattermost ni se persiste el incidente → **la mejor justificación de F2** | ✅ **respondido por F2** (2026-06-29): el replay tras matar el pod se validó en cluster (Slice 4, `reclaimed_total=1`) — la alerta ya NO se pierde; la hipótesis original describía el sistema pre-cola y F2 la resolvió |
| **E6** | Cascada doble | ChromaDB + Ollama caídos a la vez | Triple fail-open → fallback a Mattermost (no debería ser `pipeline_failed`) | Confirmar que `pipeline_failed` queda reservado para errores inesperados | ⏳ pendiente |

**Notas de preparación:**
- Los payloads de test deben incluir `startsAt` (campo obligatorio en `AlertItem`).
- Para E2/E2b: la readiness probe gatea sobre Ollama (PR-02) — si Ollama cae del todo, el agente queda `NotReady` y Alertmanager no entrega; el fail-open interno solo se ve si Ollama cae *después* de aceptar el webhook. Diseñar E2 teniendo esto en cuenta (caída mid-flight vs caída total).
- Por cada experimento rellenar: **inyección · esperado · observado · señal (delta de métrica + log + artefacto Mattermost) · veredicto** (✅ aguanta / 🟡 degrada / 🔴 gap), y screenshot para Gate 8 donde aplique.

**Nota F-06 — durabilidad de Redis (claim degradado, 2026-07-12)**: todo el estado operativo (cola Streams + PEL, escalaciones, rollbacks programados, cooldowns, índice R5) vive en un Redis **sin AOF ni PVC**. La durabilidad que SÍ se reclama: reinicio del **agente** (PEL + `_recover_rollbacks` + escalaciones re-leídas). La que NO: muerte del pod **Redis** — se pierden las escalaciones vivas y, lo más delicado, un rollback a mitad de ventana (patch aplicado sin reversión automática); las alertas firing re-entran solas por `repeat_interval` de Alertmanager. Decisión pre-chapter: **degradar el claim y declararlo** (aquí, en `docs/07` modos de fallo y en la QA 7 del guion) en vez de montar AOF+PVC con prisa. Fix real en v2.1.

---

## 2. Hallazgos del análisis de código (hipótesis a verificar)

Salieron de leer `main.py`, `diagnosis.py`, `escalation_store.py`, `config.py` y `rag.py` **antes** de tocar el cluster. Severidad provisional; confirmar en cluster con la matriz §1.

### PR-01 — Drift del default de `HTTP_TIMEOUT` ✅ (resuelto en código, 2026-06-26)
- **Evidencia**: `config.py` → `http_timeout: float = 120.0`; el cliente se crea con `httpx.AsyncClient(timeout=settings.http_timeout)` (`main.py:308`). El chaos midió **MTTR 205–252s**.
- **Resuelto en cluster**: el deployment **ya override-ea `HTTP_TIMEOUT=300`** (backlog `11`, finding E1, imagen `5aaf9f9`, 2026-05-26). Por eso el chaos no cortó.
- **Fix**: default de `config.py` alineado a **300.0** + comentario (los health-checks pasan `timeout` explícito, no afectados). Reproducibilidad local restaurada. Ningún test fijaba 120.
- **Verificar de paso** (cuando haya cluster): `kubectl get deploy agent -n arturo-llm-test -o yaml | grep -A1 HTTP_TIMEOUT`.

### PR-02 — La readiness probe convierte "Ollama caído" en "agente inalcanzable" ✅ (resuelto en F2, 2026-06-29)
- **Evidencia original**: `/readyz` chequeaba Ollama. Ollama down → pod `NotReady` → el Service deja de enrutar → Alertmanager no entrega.
- **Resuelto**: con la cola, `/readyz` pasa a chequear **Redis** (la dependencia de ingesta), no Ollama. El sentido de la cola es bufferear la lentitud/caída de Ollama, así que Ollama lento ya no saca al pod de rotación — el consumidor drena cuando vuelve. Validado en cluster: Redis a 0 → `/readyz=503`; Ollama lento → el pod sigue en rotación (la cola absorbe). Decisión "degradar-y-notificar" tomada como parte de F2.

### PR-03 — Dedup per-pod, en memoria ✅ (resuelto en F2, 2026-06-29)
- **Evidencia original**: `IN_FLIGHT_ALERTS` era un `set` en memoria del proceso, protegido por `_INFLIGHT_LOCK` → con réplicas>1 cada pod tenía su set (rompería con F3/HPA).
- **Resuelto**: el dedup migró a Redis (`SET aiops:seen:<fingerprint> NX EX <window>` dentro de `enqueue_alert`) → **cluster-wide**, sobrevive réplicas y reinicios. `IN_FLIGHT_ALERTS` retirado. F3 (HPA) ya no rompe el dedup.

### PR-04 — Outage de ChromaDB no degrada la confianza → riesgo de auto-remediación insegura 🟡
- **Evidencia**: RAG falla → `rag_failed` → el LLM sigue zero-shot. Pero `docs/10` midió **RAG safety 100% vs zero-shot 25%**. La confianza la pone el LLM solo; nada la capa a la baja en modo degradado.
- **Implicación**: durante un outage de ChromaDB, el sistema podría auto-remediar una acción incorrecta con confianza alta. La validation layer protege contra comandos *destructivos*, pero no contra un patch *erróneo pero "seguro"*.
- **Refuerzo empírico**: el finding E5 del backlog (`11`) ya observó **sobreconfianza 95–98% con razonamiento incorrecto** en qwen2.5:1.5b. Es decir, la confianza del modelo no es fiable de por sí; sin el grounding del RAG es doblemente peligroso confiar en ella.

### PR-05 — `chroma_client` ligado al arranque ✅ (resuelto en código, 2026-06-26)
- **Evidencia**: se crea una vez en el lifespan (fail-open `None` si ChromaDB no está). El caso *None-en-startup* ya lo cubría `retrieve_context` (`chroma_client or get_chroma_client()`); el gap real era el **cliente cacheado que se queda stale** si el pod ChromaDB reinicia tras el arranque → cada alerta caía a `rag_degraded` permanente hasta reiniciar el agente.
- **Fix**: en el `except` del retrieval de `_process_alert_with_diagnosis`, reconexión lazy de **un** intento: descarta el cliente stale, reintenta `retrieve_context` con `get_chroma_client()` fresco y, si va, persiste el sano en `app.state.chroma_client` (cura también `/aiops` status y `_query_recent_incidents`). Si el reintento también falla, cae al comportamiento actual (`rag_degraded`). Nuevo `outcome="rag_reconnect"` en `aiops_diagnosis_total` (self-healing visible en Grafana). 2 tests `TestRagReconnect`.

### PR-06 — Huecos de observabilidad ✅ (resuelto en código, 2026-06-25)
- **Evidencia (a)**: `timeout` y `down` del LLM compartían label `outcome="llm_failed"` → indistinguibles en Prometheus.
- **Evidencia (b)**: **no había métrica de salud de Redis**; sus fallos solo vivían en logs. El `/aiops` muestra "0 pending" cuando Redis está caído (vía `count_escalations`→0), engañoso.
- **Fix**: (a) las dos ramas de `_process_alert_with_diagnosis` emiten ahora `aiops_diagnosis_total{outcome="llm_timeout"}` (TimeoutException) y `{outcome="llm_error"}` (resto); (b) nuevo counter `aiops_escalation_store_total{outcome="stored"|"redis_down"}` en el bloque de escalación, que incrementa de forma visible durante el chaos de Redis (E3). 2 tests extendidos + 2 nuevos en `test_endpoints.py`.
- **Fuera de alcance** (quick-win): el "0 pending" engañoso de `/aiops` con Redis caído — cambio aparte en `_format_status_response` si interesa.

### PR-07 — Pérdida de alerta en restart mid-diagnóstico ✅ (resuelto en F2, 2026-06-29)
- **Evidencia original**: el webhook era fire-and-forget vía `BackgroundTasks`; un restart mataba el diagnóstico en curso y la alerta se perdía.
- **Resuelto**: el webhook encola en Redis Streams (durable). Un reinicio deja la entrada en el PEL del consumer group → `reclaim_pending` la reprocesa (replay). **Validado en cluster** (Slice 4): tras matar el pod mid-burst, `aiops_queue_reclaimed_total=1` y el diagnóstico se completó. Semántica at-least-once (posible reproceso duplicado, mitigado por dedup-key + dead-letter).

---

## 3. Plan de soluciones

Agrupado por cuándo y dónde se resuelve. Los *quick-wins* son **código puro, testeables con mocks, sin cluster** — se pueden abordar ya en sesiones de ~20 min.

| Finding | Solución propuesta | Esfuerzo | Cuándo |
|---|---|---|---|
| **PR-01** | Alinear el default de `config.py` con producción (300s — seguro porque los health-checks pasan `timeout=` explícito de 10s) y documentarlo | S | ✅ Hecho 2026-06-26 |
| **PR-04** | Cuando `rag_failed`/contexto vacío, **forzar `escalate`** (nunca auto-remediar sin grounding RAG): pasar un flag `rag_degraded` a `process_remediation`. Narrativa de demo: "el sistema sabe cuándo vuela a ciegas y se niega a auto-actuar" | M | **Quick-win prioritario** (mejora de seguridad + valor de demo) |
| **PR-05** | Reconexión lazy: si `retrieve_context` falla con el cliente cacheado, reintentar una vez con `get_chroma_client()` fresco y persistir el sano en `app.state` | S | ✅ Hecho 2026-06-26 (counter `rag_reconnect`) |
| **PR-06** | (a) Separar `outcome="llm_timeout"` vs `"llm_error"`; (b) gauge `aiops_redis_up` o counter `aiops_escalation_store_total{outcome}` | S | ✅ Hecho 2026-06-25 (counter elegido sobre gauge) |
| **PR-02** | Desacoplar readiness de Ollama: `/readyz` chequea Redis (la cola buffereará la lentitud de Ollama) | M | ✅ Hecho en F2 (2026-06-29) |
| **PR-03** | Mover el dedup a Redis (`SETNX` + TTL, cluster-wide), dueño en la capa de cola de F2 | M | ✅ Hecho en F2 (2026-06-29) |
| **PR-07** | Cola Redis Streams (at-least-once + replay) | L | ✅ Hecho en F2 (2026-06-29, validado en cluster) |

### Orden sugerido (despacio y bien)
1. **Quick-wins de código** sin cluster: PR-04 (seguridad), PR-05 (reconnect), PR-06 (observabilidad), PR-01 (drift de timeout) — **los cuatro ✅ hechos**. Cada uno con sus tests mockeados. Entran a `docs/11`.
2. **Verificación en cluster** (cuando Jay tenga sesión `kubectl`): ejecutar la matriz §1, rellenar veredictos, confirmar/descartar hipótesis, capturar PR-03 (réplicas).
3. **F2** ✅ (2026-06-29) absorbió PR-07 (replay) y PR-03 (dedup cluster-wide) y resolvió la decisión de PR-02 (readyz gated por Redis). E5 (restart mid-diagnóstico) queda demostrado en vivo como replay, ya no como pérdida.

---

## 4. Hallazgos operacionales post-F2 (cluster real, 2026-06-29)

Dos hallazgos de production-readiness que salieron **operando** la cola en cluster, no del análisis de código. Material honesto de "qué aguanta y qué falta".

### PR-08 — Durabilidad cuando Redis se recrea bajo un agente vivo ✅ (resuelto en código)
- **Síntoma observado**: alerta `HighCPU` sobre Redis. Diagnóstico inicial ("consumer educado en BLOCK 5000, el límite de 50m quedó corto") era **incorrecto**: los logs del pod viejo revelaron un **busy-spin** de `XREADGROUP failed: NOGROUP` (cientos de iter/s) tras recrear Redis (bump de recursos sin PVC → pod nuevo → stream + consumer group desaparecidos). El `except` de `consume_loop` hacía `continue` inmediato → giraba para siempre, saturando los 50m → eso disparaba el `HighCPU`. El bump a 150m solo lo parchea (si entra en spin, quema 150m igual).
- **Fix raíz** (`agent/streams.py`): `consume_loop` **self-healing** — backoff exponencial (`backoff_delay`, contador de fallos consecutivos con reset tras éxito) en vez de `continue`; si el error es `NOGROUP`, `ensure_group(start_id="$")` recrea el grupo antes del backoff. Se elige `id="$"` (no `"0"`): salta el gap de entradas durante el hueco —recuperable porque Alertmanager reenvía las firing— y evita el **replay masivo permanente** que daría `id=0` re-entregando todo el historial retenido (hasta `MAXLEN ~1000`) **sin pasar por el dedup** → 1000 diagnósticos/remediaciones sobre estados ya resueltos.
- **Lección de production-readiness**: Redis efímero (sin PVC) es aceptable para el estado durable de la cola **solo si** el consumidor se auto-recupera de la pérdida del grupo. El "fail-open" de F2 no estaba completo hasta cerrar este busy-spin. Pendiente de validar en cluster (Jay): `XGROUP DESTROY` sin reiniciar el agente → logs `NOGROUP → recreating` + CPU de Redis NO se dispara + sin replay en Mattermost.

### PR-09 — Observabilidad sin frontera de tenant en cluster compartido ✅ (resuelto en config)
- **Síntoma observado**: tiles DOWN en el panel "Scrape targets" + `HighCPU` ajeno. Al investigar: el cluster pasó de mono-usuario a **compartido** (compañera con namespace `brms-gorules`) y la observabilidad no tenía frontera. `count by (namespace) (kube_pod_info)` devolvía `arturo-*` + `brms-gorules` + `kube-system` + `cnrm-system` + ... — KSM tiene `ClusterRole` cluster-wide → veía TODO. Un OOM/CrashLoop/ImagePull en cualquier namespace ajeno disparaba **mi** Alertmanager → webhook → cola → LLM → Mattermost. La remediación estaba protegida (RBAC `Role` namespaced a `arturo-llm-test`/`arturo-chaos`), pero el **ruido + ciclos de LLM + escalaciones** sí ocurrían.
- **Fix** (config, sin código): (a) **alerting** — las 5 reglas KSM/cadvisor con `namespace=~"arturo-.*"` en el `expr`, `TargetDown` solo `job=kubernetes-endpoints`; (b) **ingestión** — KSM `--namespaces=arturo-*` (corte en origen) + cadvisor `metric_relabel keep namespace=~"arturo-.*"` (corte antes del TSDB); (c) **visualización** — panel scrape-targets a `up{job="kubernetes-endpoints"}`. Ver `docs/03` §Tenancy.
- **Lección de production-readiness**: "tu observabilidad cuando dejas de ser único tenant" es un modo de fallo real. Un `ClusterRole` de lectura + scrape `role: node` te hacen ver (y reaccionar a) workloads ajenos por defecto. Trade-off honesto asumido: cadvisor `role: node` sigue haciendo el HTTP scrape a cada nodo (incl. nodos solo-ajenos); el relabel evita el **almacenamiento**, no el **fetch**.
