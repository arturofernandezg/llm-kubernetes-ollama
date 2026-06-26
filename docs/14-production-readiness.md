# Production-Readiness — Validación F1

> **Entregable de la Fase F1** del roadmap (`docs/07`). Estado: **protocolo definido + hallazgos de análisis de código**. Los veredictos en cluster están **pendientes** (sesión sin cluster, 2026-06-25).
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
| **E4** | Dedup feliz | `for i in $(seq 1 10); do curl -s -XPOST localhost:8000/webhook/alert -H 'Content-Type: application/json' -d @burst_same.json & done; wait` (mismo alertname+ns+pod) | 1 pipeline corre, las otras 9 se saltan | `aiops_dedup_skipped_total{alertname}`=9 + 1 solo mensaje en Mattermost | ⏳ pendiente |
| **E4b** | Saturación (motivo de F2) | Mismo one-liner con N payloads **distintos** (`burst_distinct_*.json`) | N background tasks → N llamadas concurrentes a Ollama | Medir latencia p50/p99 y nº de `llm_failed` (timeouts) → **cuantificar el cuello que F2 elimina** | ⏳ pendiente |
| **E5** | Restart mid-diagnóstico | Disparar alerta y, durante el LLM (~200s), `kubectl delete pod -n arturo-llm-test -l app=agent` | **Alerta PERDIDA**: las BackgroundTasks mueren con el proceso, el webhook ya devolvió 200 | Nunca llega el mensaje a Mattermost ni se persiste el incidente → **la mejor justificación de F2** | ⏳ pendiente |
| **E6** | Cascada doble | ChromaDB + Ollama caídos a la vez | Triple fail-open → fallback a Mattermost (no debería ser `pipeline_failed`) | Confirmar que `pipeline_failed` queda reservado para errores inesperados | ⏳ pendiente |

**Notas de preparación:**
- Los payloads de test deben incluir `startsAt` (campo obligatorio en `AlertItem`).
- Para E2/E2b: la readiness probe gatea sobre Ollama (PR-02) — si Ollama cae del todo, el agente queda `NotReady` y Alertmanager no entrega; el fail-open interno solo se ve si Ollama cae *después* de aceptar el webhook. Diseñar E2 teniendo esto en cuenta (caída mid-flight vs caída total).
- Por cada experimento rellenar: **inyección · esperado · observado · señal (delta de métrica + log + artefacto Mattermost) · veredicto** (✅ aguanta / 🟡 degrada / 🔴 gap), y screenshot para Gate 8 donde aplique.

---

## 2. Hallazgos del análisis de código (hipótesis a verificar)

Salieron de leer `main.py`, `diagnosis.py`, `escalation_store.py`, `config.py` y `rag.py` **antes** de tocar el cluster. Severidad provisional; confirmar en cluster con la matriz §1.

### PR-01 — Drift del default de `HTTP_TIMEOUT` ✅ (resuelto en código, 2026-06-26)
- **Evidencia**: `config.py` → `http_timeout: float = 120.0`; el cliente se crea con `httpx.AsyncClient(timeout=settings.http_timeout)` (`main.py:308`). El chaos midió **MTTR 205–252s**.
- **Resuelto en cluster**: el deployment **ya override-ea `HTTP_TIMEOUT=300`** (backlog `11`, finding E1, imagen `5aaf9f9`, 2026-05-26). Por eso el chaos no cortó.
- **Fix**: default de `config.py` alineado a **300.0** + comentario (los health-checks pasan `timeout` explícito, no afectados). Reproducibilidad local restaurada. Ningún test fijaba 120.
- **Verificar de paso** (cuando haya cluster): `kubectl get deploy agent -n arturo-llm-test -o yaml | grep -A1 HTTP_TIMEOUT`.

### PR-02 — La readiness probe convierte "Ollama caído" en "agente inalcanzable" 🟠
- **Evidencia**: `/readyz` chequea Ollama (`main.py:434`). Ollama down → pod `NotReady` → el Service deja de enrutar → Alertmanager no entrega.
- **Implicación**: el "fail-open de LLM" solo se dispara en una ventana estrecha (Ollama cae tras aceptar el webhook, antes de la llamada). Si cae del todo, las alertas se quedan en Alertmanager (que reintenta con backoff y acaba descartando), no degradan dentro del agente.
- **Decisión de diseño** (no es un bug): mantener el gating (*fail-safe*, pero silencioso) vs desacoplar readiness de las dependencias (degradar-y-notificar). Interactúa con F2 (con cola, las alertas no se pierden en el outage).

### PR-03 — Dedup per-pod, en memoria 🟠
- **Evidencia**: `IN_FLIGHT_ALERTS` es un `set` en memoria del proceso (`main.py:128`), protegido por `_INFLIGHT_LOCK`.
- **Implicación**: con 1 réplica funciona; **F3 (HPA) escalaría el agente y rompería el dedup** (cada pod con su set). Un descubrimiento de F1 que condiciona F3.
- **Verificar**: nº de réplicas actual del deployment `agent`.

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

### PR-07 — Pérdida de alerta en restart mid-diagnóstico 🔴 (ya documentado)
- **Evidencia**: el webhook es fire-and-forget vía `BackgroundTasks`; un restart mata el diagnóstico en curso y la alerta se pierde (ya en "Modos de fallo conocidos" de `docs/07`).
- **No requiere acción nueva**: **es la justificación de F2** (cola Redis Streams). E5 lo demuestra en vivo.

---

## 3. Plan de soluciones

Agrupado por cuándo y dónde se resuelve. Los *quick-wins* son **código puro, testeables con mocks, sin cluster** — se pueden abordar ya en sesiones de ~20 min.

| Finding | Solución propuesta | Esfuerzo | Cuándo |
|---|---|---|---|
| **PR-01** | Alinear el default de `config.py` con producción (300s — seguro porque los health-checks pasan `timeout=` explícito de 10s) y documentarlo | S | ✅ Hecho 2026-06-26 |
| **PR-04** | Cuando `rag_failed`/contexto vacío, **forzar `escalate`** (nunca auto-remediar sin grounding RAG): pasar un flag `rag_degraded` a `process_remediation`. Narrativa de demo: "el sistema sabe cuándo vuela a ciegas y se niega a auto-actuar" | M | **Quick-win prioritario** (mejora de seguridad + valor de demo) |
| **PR-05** | Reconexión lazy: si `retrieve_context` falla con el cliente cacheado, reintentar una vez con `get_chroma_client()` fresco y persistir el sano en `app.state` | S | ✅ Hecho 2026-06-26 (counter `rag_reconnect`) |
| **PR-06** | (a) Separar `outcome="llm_timeout"` vs `"llm_error"`; (b) gauge `aiops_redis_up` o counter `aiops_escalation_store_total{outcome}` | S | ✅ Hecho 2026-06-25 (counter elegido sobre gauge) |
| **PR-02** | Decisión de diseño: desacoplar readiness de las dependencias (degradar-y-notificar) vs mantener fail-safe. Recomendación: decidir **junto con F2** (la cola cambia el cálculo) | M | Diseño — decidir en F2 |
| **PR-03** | Mover el dedup a Redis (`SETNX` + TTL, cluster-wide). Converge con F2: **que la capa Redis de F2 sea dueña del dedup**. Hasta entonces, mantener `agent` a `replicas=1` y documentar el constraint | M | **Plegar en F2** |
| **PR-07** | Cola Redis Streams (at-least-once + replay) | L | **Es F2** |

### Orden sugerido (despacio y bien)
1. **Quick-wins de código** sin cluster: PR-04 (seguridad), PR-05 (reconnect), PR-06 (observabilidad), PR-01 (drift de timeout) — **los cuatro ✅ hechos**. Cada uno con sus tests mockeados. Entran a `docs/11`.
2. **Verificación en cluster** (cuando Jay tenga sesión `kubectl`): ejecutar la matriz §1, rellenar veredictos, confirmar/descartar hipótesis, capturar PR-03 (réplicas).
3. **F2** absorbe PR-07 y PR-03; la decisión de PR-02 se toma en su diseño.
