# Quality Backlog — AIOps Agent

Backlog vivo de findings de calidad de código. Cada fila: `ID | severidad | localización | descripción | fix propuesto | estado`.

Estados: `TODO` | `DONE` | `WONTFIX`

Workflow: ver `docs/08-code-quality-playbook.md`.

---

## Sesión #1 — Fixes high (2026-05-11)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| H1 | high | remediation.py:318-359 | `proc` sin inicializar antes del try: si `create_subprocess_exec` lanza `TimeoutError`, `proc.kill()` en el handler falla con `UnboundLocalError` | `proc = None` antes del try; guard `if proc is not None` en TimeoutError handler | DONE |
| H2 | high | remediation.py:353 | `except Exception: pass` en kill de proceso swallows errores sin log | `except Exception as kill_exc: logger.debug(...)` | DONE |
| H3 | high | main.py:80,88,372,509 | `PENDING_ESCALATIONS` mutado desde pipeline de alerta y callback de botón sin lock; `RuntimeError` posible si cleanup itera mientras se inserta | `_PENDING_LOCK = asyncio.Lock()`, wrap mutaciones en `async with _PENDING_LOCK` | DONE |
| H4 | high | main.py:466 | `pop` antes de TTL check: si TTL expiró pero cleanup no corrió, entry se elimina antes de validar → imposible retry si execution falla después | Reordenar: `get()` → TTL check → `pop()` dentro del lock | DONE |
| H5 | high | main.py:83-89 | Cleanup solo se ejecuta dentro del pipeline de alerta (pasivo); en clusters de baja actividad entries expiradas persisten indefinidamente | Background task `_periodic_cleanup()` en lifespan cada 300s | DONE |

---

## remediation.py — medium/low (sesión #2, 2026-05-11)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| R1 | medium | remediation.py:154-179 | `classify_command` no valida que el input sea `str`; si llega `None` o no-string, `cmd.strip()` lanza `AttributeError` | Early return `if not isinstance(cmd, str): return CommandSafety.UNKNOWN` | DONE |
| R2 | medium | remediation.py:182-196 | `validate_commands` itera `commands` sin filtrar no-strings ni `None` | Coerce/skip no-strings con `logger.warning` | DONE |
| R3 | medium | remediation.py:244-267 | Regla 4.6: `proposed_action.get("current_value", "")` → string vacío causa `ValueError` en `parse_memory_to_bytes`, capturado como `unparseable_memory`. Oculta la causa real (campo ausente vs valor inválido) | Validar presencia de `current_value`/`new_value` explícitamente con `reason_code` distinto (`missing_memory_value`) | DONE |
| R4 | medium | remediation.py:309,314,354,368 + CancelledError handler | Logger usa `%-formatting` en 4 puntos; `except Exception: pass` en kill de CancelledError sin log | Convertir a `extra={}` estructurado; `except Exception as kill_exc: logger.debug(...)` | DONE |
| R5 | low | remediation.py:405 | `executed: action == AUTO_REMEDIATE` es `True` en dry-run aunque no hubo ejecución real | Renombrado a `execution_attempted` (también actualizado en test_remediation.py y test_rag.py) | DONE |
| R6 | low | remediation.py:422 | `commands: list[str] = diagnosis.get("commands") or []` — type hint dice `list[str]` pero no valida el contenido | Filtrar no-strings en `process_remediation` con `logger.warning` | DONE |
| R7 | medium | remediation.py:392-395 + 428-431 | `safe_commands` list comprehension duplicada 2× (idéntica en `build_remediation_result` y `process_remediation`) | Extraer helper `_get_safe_commands(validations)` | DONE |
| R8 | medium | remediation.py:252 | Regla 4.6: si `current_value` parsea a `0` bytes, `new_bytes > 2 * 0` es siempre True → cualquier cambio bloquea | Guard: si `current_bytes == 0`, devolver ESCALATE con `reason_code: zero_current_memory` | DONE |

---

## main.py — medium/low (pendiente sesión #3)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| M1 | medium | main.py:27 | `ActionCallbackContext` importado pero nunca referenciado directamente en main.py | Eliminar import | DONE |
| M2 | low | main.py:85 | `datetime.now()` naive en cleanup; si el host cambia TZ, el math de TTL deriva | Usar `datetime.now(timezone.utc)` consistente con el resto | DONE |
| M3 | medium | main.py:220-272 | `diagnosis["confidence"]` y `diagnosis["risk"]` accedidos directamente (KeyError si falta); contrasta con estilo `.get()` del resto | `diagnosis.get("confidence", 0.0)` / `diagnosis.get("risk", "high")` | DONE |
| M4 | medium | main.py:284-299 | `_format_escalation_body` accede `diagnosis['diagnosis']`, `diagnosis['confidence']`, `diagnosis['risk']` directamente | Mismo fix `.get()` defensivo | DONE |
| M5 | medium | main.py:226-232,277-281,396-399 | Extracción de `severity/pod/namespace/alert_name` duplicada 4× en formatters | Extraer `_extract_alert_meta(alert) -> tuple` helper | DONE |
| M6 | medium | main.py:491 | `else` de `action == "approve"` acepta cualquier valor como reject (typos, payloads malformados) | `elif action == "reject"` + 400 en acción desconocida | DONE |
| M7 | medium | main.py:494 | Excepción en `execute_commands` loguea el error pero no incrementa counter de fallo | `REMEDIATION_COUNTER.labels(action="human_approve_failed").inc()` | DONE |
| M8 | low | main.py:316 | `DIAGNOSIS_COUNTER.labels(outcome="rag_ok")` no está en la docstring de labels del counter (L52) | Añadir `rag_ok` a docstring o renombrar | DONE |
| M9 | low | main.py:497,507 | Labels `human_approved`/`human_rejected` en `REMEDIATION_COUNTER` no declarados en docstring (L57) | Actualizar docstring | DONE |
| M10 | medium | main.py:309 | Outer `try/except Exception` en `_process_alert_with_diagnosis` demasiado amplio; puede enmascarar bugs en el fallback mismo | Fallback `send_mattermost_alert` envuelto en su propio try/except con log estructurado | DONE |

---

## mattermost.py — medium/low (sesión #4, 2026-05-11)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| MM1 | medium | mattermost.py:18 | `_post_with_retry` crea nuevo `httpx.AsyncClient` por llamada — no reutiliza el cliente compartido de `app.state` | Aceptar parámetro `client` o reutilizar cliente compartido | WONTFIX (MVP — `background_tasks.add_task` dificulta inyección del cliente sin refactor mayor) |
| MM2 | low | mattermost.py:20 | `hasattr(settings, 'mattermost_webhook_url')` es dead code: Settings de Pydantic siempre tiene el atributo | Eliminar check `hasattr`, dejar solo check de string vacío | DONE |
| MM3 | medium | mattermost.py:31,35,42,47,53 | f-strings en logger; inconsistente con `extra={}` estructurado del resto del proyecto | Convertir a `logger.info("...", extra={"attempt": ...})` | DONE |
| MM4 | medium | mattermost.py:41 | `except Exception: return False` swallows `TypeError`/`KeyError` de payload malformado — caller no puede distinguir error de config vs red | Narrow exception o re-raise inesperados | DONE (fail-open mantenido; log estructurado con error_type) |
| MM5 | medium | mattermost.py:72-115 | `send_escalation_with_buttons`: no validación de longitud de `attachment_text` (límite Mattermost 16KB) | Truncar `attachment_text` si supera umbral | DONE (_MAX_TEXT_LENGTH = 14_000) |
| MM6 | low | mattermost.py:72 | Parámetro `safe_commands` en `send_escalation_with_buttons` nunca se usa en el cuerpo de la función | Eliminar parámetro o usarlo para renderizar botones por comando | DONE (parámetro eliminado + caller main.py actualizado) |
| MM7 | low | mattermost.py:86-112 | Color `#FF6600` y labels de botones hardcodeados | Extraer como constantes de módulo | DONE (_ATTACHMENT_COLOUR, _BTN_APPROVE_LABEL, _BTN_REJECT_LABEL) |
| MM8 | low | mattermost.py:60-69 | `send_mattermost_alert`: no truncation guard para `message` muy largo (límite 16KB Mattermost) | Truncar con ellipsis si supera umbral | DONE (_MAX_TEXT_LENGTH compartido con MM5) |
| MM9 | medium | mattermost.py:34-37 | 4xx path hace `break` sin asignar `last_exc` → log final dice "Last error: None" aunque el error se logueó antes | `return False` directamente en 4xx (no `break`) para evitar el log final engañoso | DONE |

---

## Cross-cutting (sesión #5, 2026-05-11)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| X1 | medium | main.py:327, 440 | Consolidación de extracción `alert_name`: 2 inline `alert.labels.get("alertname")` reemplazadas con llamadas a helper `_extract_alert_meta(alert)` existente | Usar `_extract_alert_meta` consistentemente en ambas call sites | DONE |
| X2 | medium | mattermost.py:_post_with_retry / main.py:574-613 | Retry con exponential backoff implementado 2× con detalles divergentes | `backoff_delay(attempt, base, max_delay)` helper en `utils.py`; usado en ambos módulos (full shared wrapper no viable: semántica divergente — bool vs HTTPException) | DONE |
| X3 | low | main.py:/extract endpoint — líneas 594–665 | 7 logger calls embebían `request_id` como `[%s]` en message strings; inconsistente con `extra={}` estructurado | Estandarizar a `extra={"request_id": request_id, ...}` en 7 calls | DONE |
| X4 | high | main.py:80 | `PENDING_ESCALATIONS` perdido en restart de pod — decisiones humanas pendientes desaparecen silenciosamente | Redis persistence via `escalation_store.py` (2026-05-27): `store/get/delete/count` async, TTL nativo, fail-open. `k8s/redis.yaml` nuevo. `PENDING_ESCALATIONS` dict eliminado. | DONE 2026-05-27 (desplegado, imagen `fd37a5d`) |
| X5 | medium | main.py:488 | Callback de Mattermost no verifica HMAC/shared secret — cualquiera que conozca el endpoint puede aprobar remediaciones | HMAC-SHA256 por botón (incident_id:action firmado con webhook_secret); `_verify_hmac_token()` en `/webhook/action`; `optional: true` en K8s Secret para backward compat | DONE |

---

## Seguridad — sesión #7 (2026-05-12)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| S2 | high | rag.py:47 | `response.json()["embedding"]` sin guard — si Ollama devuelve error/formato inesperado, `KeyError` directo | `data.get("embedding")` + `ValueError` con contexto si ausente o vacío | DONE |
| S3 | medium | main.py:142, 221 | `m["name"]` en list comprehension sobre respuesta Ollama `/api/tags` — `KeyError` si alguna entrada no tiene `"name"` | `m.get("name", "")` con filtrado de entradas sin nombre | DONE |
| S4 | medium | main.py:87-88 | `_verify_hmac_token` devuelve `True` si `webhook_secret=""` — bypass total sin warning | Warning explícito en lifespan startup; bypass mantenido para backward compat (K8s Secret `optional: true`) | DONE |

---

## Chaos script — (2026-05-25/26)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| C1 | low | scripts/chaos.sh:_wait_for_agent_log | Búsqueda de log usa `--since=10m` sin filtrar por T0 — encuentra entradas anteriores al experimento actual y produce MTTD negativo | Pasar epoch T0 + filtrar ts>=T0 + head -1 | FIXED 2026-05-26 |
| C2 | high | k8s/chaos/chaos-cpu-stress.yaml:47 | `stress-ng` en imagen que solo tiene `stress` — pod no arranca, HighCPU nunca dispara | `stress-ng`→`stress`, `600s`→`600` | FIXED 2026-05-26 |
| Q1 | medium | scripts/chaos.sh:_wait_for_agent_log | `--since=10m --tail=200` es una ventana rodante: en HighCPU (~360s) la línea de detección puede salir de las 200 últimas; MTTR se leía a ojo de Mattermost | `--since-time=T0-10s` (kubectl RFC3339); nueva `_wait_for_chaos_metrics` (no-fatal) captura MTTD/MTTR/outcome del log "Chaos metrics recorded" | FIXED 2026-05-26 |

## Prometheus KSM label collision — (2026-05-26)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| P1 | high | k8s/prometheus.yaml:kubernetes-endpoints | Prometheus relabela `namespace=arturo-monitoring` y `pod=kube-state-metrics-xxx` al scrapeear KSM, ocultando el namespace/pod real en `exported_*`. Consecuencias: (1) join HighCPU/HighMemory falla → alertas nunca disparan; (2) mensajes Mattermost muestran pod de KSM en lugar del pod afectado | `metric_relabel_configs`: `exported_namespace→namespace` + labeldrop, `exported_pod→pod` + labeldrop | FIXED + verified 2026-05-26 (aplicado en cluster; los 4 experimentos chaos dispararon `is_chaos` con namespace correcto) |

---

## Sesión pruebas E2E — findings de ejecución (2026-05-26)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| E1 | medium | k8s/deployment-agent.yaml:HTTP_TIMEOUT | `HTTP_TIMEOUT=240` demasiado ajustado: la inferencia de qwen2.5:1.5b en CPU llega a ~205–273s. BadImage superó 240s → `httpx.ReadTimeout` → `diagnosis=None` → `no_diagnosis` (fail-open OK: alerta cruda enviada). Margen p~max ≈ timeout | `HTTP_TIMEOUT` 240→300; re-run BadImage MTTR 273.5s → escalate | FIXED 2026-05-26 (imagen `5aaf9f9`) |
| E2 | low | main.py:594,606,616 | Warnings del pipeline de diagnóstico logueaban `%s` sobre `exc`; las excepciones de timeout de httpx serializan a string vacío → `"Diagnosis generation failed for X: "` sin causa (black-box, contra principio de observabilidad) | `%s`→`%r` para mostrar el tipo (`ReadTimeout('')`) | FIXED 2026-05-26 |
| E3 | medium | main.py:743 (handle_alert_webhook) | No hay guard de dedup in-flight: cada alerta `firing` lanza un pipeline de diagnóstico aunque ya haya uno corriendo para el mismo `alertname+pod`. Re-sends de Alertmanager + flapping de reglas `increase[15m]` saturan el LLM single-thread → contención y timeouts | Guard `IN_FLIGHT_DIAGNOSES` keyed en `alertname+pod`; skip si ya en curso. Requiere tests | DONE (FASE 2, 2026-05-27 — `IN_FLIGHT_ALERTS` + counter `aiops_dedup_skipped_total`, `TestInFlightDedup`) |
| E4 | medium | rag.py (feedback loop) / ChromaDB | Contaminación del RAG: incidentes registrados durante el bug de label-collision (pod=`kube-state-metrics`, NS=`arturo-monitoring`) se ingestaron en ChromaDB. Diagnósticos posteriores los recuperan y alucinan el pod/namespace erróneo en el texto (ej. BadImage re-run 4:36 PM: header correcto pero texto cita `kube-state-metrics … arturo-monitoring`) | Validar/deduplicar incidentes en ingest; purgar incidentes pre-fix de ChromaDB (metadata `namespace=arturo-monitoring` + chaos); considerar versionar el store ante cambios de schema/labels | DONE (2026-05-27 — 92 incidents contaminados purgados; backup `chromadb-backup-clean-20260527.tar.gz`) |
| E5 | low | diagnosis.py (qwen2.5:1.5b) | Sobreconfianza: confidence 95–98% con razonamiento free-text incorrecto (HighCPU → "inadequate memory allocation"; CrashLoop → "insufficient memory limits" para un `/bin/false`). Los campos estructurados (pod/NS/alert) sí son correctos | Limitación del modelo pequeño en CPU. Future work: modelo mayor o calibración; o ponderar confidence con coherencia alert↔diagnóstico | WONTFIX (MVP — limitación conocida documentada) |

---

## Production-readiness — análisis F1 (2026-06-25)

> Hallazgos del análisis de código de la Fase F1. Detalle, contexto y matriz de validación en `docs/14-production-readiness.md`. Aquí solo los **accionables en código**; PR-02 (decisión readiness), PR-03 (dedup→F2) y PR-07 (pérdida de alerta→F2) viven en `docs/14` como diseño/fase, no como findings de código.

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| PR-01 | low | config.py:24 | Default `http_timeout=120.0` no coincide con el desplegado (`HTTP_TIMEOUT=300`, backlog E1). Local con el default verá timeouts que en cluster no pasan → reproducibilidad rota | Alinear default a 300.0 (seguro: los health-checks pasan `timeout=10.0` explícito) + comentario | DONE (2026-06-26 — default `http_timeout=300.0` + comentario; ningún test fijaba 120) |
| PR-04 | medium | main.py:635-668 (`_process_alert_with_diagnosis`) | Outage de ChromaDB (`rag_failed`) no degrada la confianza; el LLM sigue zero-shot (safety 25% vs 100% RAG, `docs/10`) y es sobreconfiado (E5) → riesgo de auto-remediación insegura sin grounding | Pasar flag `rag_degraded` a `process_remediation`; forzar `escalate` (nunca auto-remediar sin contexto RAG). Tests mockeados | DONE (2026-06-25 — `decide_action` regla 7.5 + flag propagado desde main.py; 3 tests `rag_degraded`; 137 passed) |
| PR-05 | low | main.py (`_process_alert_with_diagnosis`) | `chroma_client` se liga en startup; un cliente cacheado se queda stale si el pod ChromaDB reinicia tras el arranque → cada alerta cae a `rag_degraded` permanente hasta reiniciar el agente (el caso None-en-startup ya lo cubría `retrieve_context` con su `or get_chroma_client()`) | Reconexión lazy en el `except` del retrieval: descarta el cliente stale, reintenta una vez con `get_chroma_client()`, persiste el sano en `app.state.chroma_client` (cura status + alertas siguientes + `_query_recent_incidents`) | DONE (2026-06-26 — reintento único + persistencia; nuevo `outcome="rag_reconnect"` en `aiops_diagnosis_total`; 2 tests `TestRagReconnect`) |
| PR-06 | low | main.py:58-61,105 / escalation_store.py | (a) timeout y down del LLM comparten `outcome="llm_failed"` → indistinguibles en Prometheus; (b) sin métrica de salud de Redis (fallos solo en logs; `/aiops` muestra "0 pending" si Redis cae) | (a) separar `llm_timeout`/`llm_error`; (b) gauge `aiops_redis_up` o counter `aiops_escalation_store_total{outcome}` | DONE (2026-06-25 — (a) `aiops_diagnosis_total` ahora emite `llm_timeout`/`llm_error`; (b) nuevo counter `aiops_escalation_store_total{outcome="stored"\|"redis_down"}` en el bloque de escalación; 2 tests extendidos + 2 nuevos. Fuera de alcance: el "0 pending" engañoso de `/aiops`) |

---

## Notas

- Los `WONTFIX` reflejan decisiones conscientemente aceptadas para el MVP del TFG.
- Los findings de sesión #1 tienen tests añadidos que documentan el comportamiento correcto.
- La sesión #8 (k8s nodeSelector guaranteed + docs sync) no tiene findings en este backlog — es una tarea de infraestructura/docs, no de calidad de código. Sesiones #1-#8 completadas.
- X2: `backoff_delay` helper en `utils.py` consolida el cálculo de delay; wrapper completo no extraído porque las semánticas son incompatibles (mattermost retorna bool, /extract lanza HTTPException).
