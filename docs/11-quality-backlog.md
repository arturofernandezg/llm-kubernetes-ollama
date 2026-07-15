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
| E3 | medium | main.py:743 (handle_alert_webhook) | No hay guard de dedup in-flight: cada alerta `firing` lanza un pipeline de diagnóstico aunque ya haya uno corriendo para el mismo `alertname+pod`. Re-sends de Alertmanager + flapping de reglas `increase[15m]` saturan el LLM single-thread → contención y timeouts | Guard `IN_FLIGHT_DIAGNOSES` keyed en `alertname+pod`; skip si ya en curso. Requiere tests | DONE (FASE 2, 2026-05-27 — `IN_FLIGHT_ALERTS` + counter `aiops_dedup_skipped_total`, `TestInFlightDedup`). **Superado por F2 (2026-06-29)**: el dedup migró a Redis (`SET aiops:seen:<fp> NX EX`, cluster-wide) en `enqueue_alert`; `IN_FLIGHT_ALERTS`/`TestInFlightDedup` retirados |
| E4 | medium | rag.py (feedback loop) / ChromaDB | Contaminación del RAG: incidentes registrados durante el bug de label-collision (pod=`kube-state-metrics`, NS=`arturo-monitoring`) se ingestaron en ChromaDB. Diagnósticos posteriores los recuperan y alucinan el pod/namespace erróneo en el texto (ej. BadImage re-run 4:36 PM: header correcto pero texto cita `kube-state-metrics … arturo-monitoring`) | Validar/deduplicar incidentes en ingest; purgar incidentes pre-fix de ChromaDB (metadata `namespace=arturo-monitoring` + chaos); considerar versionar el store ante cambios de schema/labels | DONE (2026-05-27 — 92 incidents contaminados purgados; backup `chromadb-backup-clean-20260527.tar.gz`) |
| E5 | low | diagnosis.py (qwen2.5:1.5b) | Sobreconfianza: confidence 95–98% con razonamiento free-text incorrecto (HighCPU → "inadequate memory allocation"; CrashLoop → "insufficient memory limits" para un `/bin/false`). Los campos estructurados (pod/NS/alert) sí son correctos | Limitación del modelo pequeño en CPU. Future work: modelo mayor o calibración; o ponderar confidence con coherencia alert↔diagnóstico | WONTFIX (MVP — limitación conocida documentada) |

---

## Production-readiness — análisis F1 (2026-06-25)

> Hallazgos del análisis de código de la Fase F1. Detalle, contexto y matriz de validación en `docs/14-production-readiness.md`. Aquí solo los **accionables en código**; PR-02 (readiness), PR-03 (dedup) y PR-07 (pérdida de alerta) viven en `docs/14` como diseño/fase — **los tres ✅ resueltos en F2 (2026-06-29)**.

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| PR-01 | low | config.py:24 | Default `http_timeout=120.0` no coincide con el desplegado (`HTTP_TIMEOUT=300`, backlog E1). Local con el default verá timeouts que en cluster no pasan → reproducibilidad rota | Alinear default a 300.0 (seguro: los health-checks pasan `timeout=10.0` explícito) + comentario | DONE (2026-06-26 — default `http_timeout=300.0` + comentario; ningún test fijaba 120) |
| PR-04 | medium | main.py:635-668 (`_process_alert_with_diagnosis`) | Outage de ChromaDB (`rag_failed`) no degrada la confianza; el LLM sigue zero-shot (safety 25% vs 100% RAG, `docs/10`) y es sobreconfiado (E5) → riesgo de auto-remediación insegura sin grounding | Pasar flag `rag_degraded` a `process_remediation`; forzar `escalate` (nunca auto-remediar sin contexto RAG). Tests mockeados | DONE (2026-06-25 — `decide_action` regla 7.5 + flag propagado desde main.py; 3 tests `rag_degraded`; 137 passed) |
| PR-05 | low | main.py (`_process_alert_with_diagnosis`) | `chroma_client` se liga en startup; un cliente cacheado se queda stale si el pod ChromaDB reinicia tras el arranque → cada alerta cae a `rag_degraded` permanente hasta reiniciar el agente (el caso None-en-startup ya lo cubría `retrieve_context` con su `or get_chroma_client()`) | Reconexión lazy en el `except` del retrieval: descarta el cliente stale, reintenta una vez con `get_chroma_client()`, persiste el sano en `app.state.chroma_client` (cura status + alertas siguientes + `_query_recent_incidents`) | DONE (2026-06-26 — reintento único + persistencia; nuevo `outcome="rag_reconnect"` en `aiops_diagnosis_total`; 2 tests `TestRagReconnect`) |
| PR-06 | low | main.py:58-61,105 / escalation_store.py | (a) timeout y down del LLM comparten `outcome="llm_failed"` → indistinguibles en Prometheus; (b) sin métrica de salud de Redis (fallos solo en logs; `/aiops` muestra "0 pending" si Redis cae) | (a) separar `llm_timeout`/`llm_error`; (b) gauge `aiops_redis_up` o counter `aiops_escalation_store_total{outcome}` | DONE (2026-06-25 — (a) `aiops_diagnosis_total` ahora emite `llm_timeout`/`llm_error`; (b) nuevo counter `aiops_escalation_store_total{outcome="stored"\|"redis_down"}` en el bloque de escalación; 2 tests extendidos + 2 nuevos. Fuera de alcance: el "0 pending" engañoso de `/aiops`) |

---

## Auditoría arquitectónica v2 (2026-07-01)

> Revisión senior (rol arquitecto DevOps/SRE/MLOps), 5.9/10, "rechazo para producción". Informe modo-libro: `docs_sesion/2026-07-01-auditoria-arquitectura.html`. Reencuadra el trabajo pendiente como **v2** (3 ejes: A grounding / B confianza / C cobertura).

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| P0·1 | **blocker** | main.py / diagnosis.py | **Diagnóstico a ciegas**: `generate_diagnosis()` solo recibe labels+annotations+RAG; cero `kubectl get/logs/events` antes del LLM (tiene el RBAC y no lo usa). El target/`current_value` salen del LLM → el 1.5b los alucina (causa raíz de la slice C de F3) | `agent/enrichment.py`: gather K8s fail-soft **antes** del LLM; `current_value`/target desde el snapshot. Unifica comando humano/auto | DONE (2026-07-02, `b23dc74`+`cb2d1db` — enrichment + seal + cluster facts al prompt + confidence grounded + regla 4.7; suite ~535 verde en Cloud Build) |
| P0·2 | **blocker** | main.py:150 (`_verify_hmac_token`), 1055 (`/webhook/command`); secrets-setup.sh | **Auth fail-open**: secret vacío → verificación omitida → remediación no autenticada con `DRY_RUN=false`. Agravado por mismatch de `secrets-setup.sh` (crea claves equivocadas, nunca los tokens) | Fail-open gated por `remediation_dry_run` (real ⇒ 401); startup log `error`; reescribir `secrets-setup.sh` con las claves reales del deployment | DONE (2026-07-01 — +3 tests; `test_endpoints -k` 10 passed) |
| P0·3 | **blocker** | main.py:146 (`IN_FLIGHT_ROLLBACKS`) | **Rollback no durable**: dict en memoria + `sleep(300)`; si el pod reinicia en la ventana, el patch queda aplicado y nunca se revierte. Incoherente con la cola durable de F2 | Persistir `RollbackContext` en Redis (`rollback_store.py`); `_recover_rollbacks()` al arranque; `_evaluate_rollback` duerme el tiempo restante | DONE (2026-07-01 — nuevo módulo + recovery; +11 tests; `test_rollback.py` 22 passed) |
| A1 | high | remediation.py (`check_pod_health`) | Selector `-l app={name}` hardcodeado: si el deployment usa otra label → `no_pods_found` → `healthy=False` → rollback falso sobre una remediación que funcionó | Derivar el selector del `.spec.selector` real | DONE (2026-07-02 — `match_labels` del snapshot threaded vía `proposed_action`; fallback `app={name}` solo sin enrichment) |
| A2 | medium | main.py (approve humano) | El approve humano ejecuta `incident.safe_commands` (free-text del LLM, a menudo no ejecutables), no el `build_set_resources_command` determinista | Unificar: humano y auto comparten el comando sintetizado por el motor | DONE (2026-07-02, `cb2d1db` — `structured_command` sintetizado una vez en `process_remediation`; escalación lo guarda/enseña; approve captura snapshot + programa rollback reusando `incident_id`) |
| A3 | medium | rag.py (ingest_incident) | **Poisoning RAG diferido**: los incidentes se persisten desde salidas del LLM (incl. `raw_response`) y se reinyectan como contexto | Gate de calidad en la ingesta (F4 bucle de aprendizaje) | TODO (F4) |
| A4 | low | Dockerfile / Fase 0 | Código muerto Fase 0 (`tf_generator`, `extraction`, `validation`, `/extract`) shippeado en el binario de producción; `.DS_Store`/`.pytest_cache` en repo | Excluir del build de producción; limpiar artefactos | TODO |

---

## Review senior post-hardening (2026-07-02)

> Segunda review senior sobre `cb2d1db` (post P0/P1): **7.1/10**, 18 hallazgos F-01..F-18. Informe modo-libro: `docs_sesion/estado_de_proyecto.html (antes 2026-07-02-paper-review-senior.html)`. Los 6 **Alta** son los pendientes reales; aquí los trabajados + los abiertos relevantes.

| ID | Severidad | Localización | Descripción | Fix | Estado |
|---|---|---|---|---|---|
| F-05 | Alta | streams.py (`enqueue_alert`) | XADD fallido tras SETNX deja dedup-key huérfana → el retry de Alertmanager recibe 200 "duplicado benigno" sin encolar → alerta perdida hasta `repeat_interval` (agujero en el claim "no se pierde ninguna alerta") | Compensación: borrar la dedup-key antes de propagar (elegida sobre invertir SETNX/XADD, que abre ventana de duplicados en ráfagas). Delete de compensación fail-soft con warning | DONE (2026-07-02; +2 tests 2026-07-03) |
| F-02 | Alta | remediation.py (`seal_proposed_action`) | Kind resuelto pero no aplicado: un pod de StatefulSet confirmado disparaba el camino auto completo → `kubectl set resources deployment <nombre-sts>` → NotFound (el MISMO síntoma que v2 anuncia como resuelto) | Gate estricto `workload_kind == "Deployment"` (STS/DS/None → anula PA + `target_unresolved` → 4.7 escala). Estricto incluye None: producción nunca produce name-sin-kind | DONE (2026-07-02, +1 test parametrizado) |
| F-01 | Alta | remediation.py (`process_remediation`) | Sin cooldown por workload: alerta re-firing dentro de la ventana de rollout/rollback → segundo patch 2× encima del primero (patch-storm) | `acquire_workload_cooldown` (SETNX `aiops:cooldown:{ns}/{name}` TTL `remediation_cooldown_seconds=600` > ventana rollback) solo en la rama auto estructurada; bloqueado o Redis error → ESCALATE fail-closed (`reason_code=workload_cooldown`); `redis_client=None` → sin gate. Gate fuera de `decide_action` (puro, sin I/O) | DONE (2026-07-03, +6 tests). **Decisión cerrada de facto (chaos 2026-07-04)**: el approve humano SÍ debe sembrar cooldown — una doble escalación OOM+CrashLoop sobre el mismo pod lo demostró en vivo (F-01 solo cubre auto). Extensión pendiente (ver hallazgos cluster abajo) |
| F-04 | Alta | mattermost.py (`_post_with_retry`) | Cliente MM heredaba `http_timeout=300s` (tamaño LLM) → con MM caído, 3 retries bloqueaban minutos | `mattermost_timeout=10.0` dedicado | DONE (2026-07-03, +1 test) |
| F-03 | Alta | main.py / rag.py | Llamadas ChromaDB síncronas (bloquean el event loop) | `asyncio.to_thread` en TODAS las llamadas HTTP de chroma (`retrieve_context` inner `_blocking`, `ingest_*` vía `_upsert`, `ensure_collections` en lifespan, query del slash) | DONE (2026-07-04, +2 tests `TestChromaOffloading`; en `8a40fdc`) |
| F-06 | Alta | k8s/redis.yaml | Redis sin AOF ni PVC — la durabilidad de la cola/rollbacks no sobrevive un reinicio de Redis | AOF+PVC o degradar el claim en docs | ✅ DONE por vía docs (2026-07-12, S6): claim degradado y declarado — la durabilidad cubre reinicios del **agente**, NO la muerte del pod Redis; fila propia en modos de fallo (`docs/07`), nota en `docs/14` y QA 7 del guion v3. AOF+PVC (patrón ChromaDB) queda en v2.1 |
| F-11 | Media | main.py | `incident_id` se genera tarde (al escalar), no en la ingesta → trazabilidad parcial | Generarlo al encolar | TODO |
| F-17 | Media | enrichment.py / diagnosis.py | El gather no trae logs+events del pod (el prompt razona sin ellos) | Añadir `kubectl logs --tail` + events al snapshot/prompt — primer uso genuinamente nuevo del LLM | DONE (2026-07-10): `IncidentSnapshot.logs_tail`+`recent_events`; `_gather_logs` (`--previous` si el contenedor murió, fallback a current, cap líneas+chars) + `_gather_events` (`--field-selector involvedObject.name`, newest-last, límite); helper `_kubectl_text` para logs no-JSON; `format_cluster_facts` con bloques RECENT EVENTS/RECENT LOGS; settings `enrichment_log_tail_lines/max_chars/events_limit`. Todo fail-soft. +~14 tests |
| F-16 | Media | k8s/secrets-setup.sh | Script-plantilla sin guard → ejecutado con placeholders pisó el secret real (incidente 2026-07-02) | `abort_if_placeholder` antes de tocar kubectl | DONE (2026-07-02) |

---

## Hallazgos de la validación v2 en cluster (2026-07-04 + run `cured` 2026-07-06)

> Del arco completo en cluster (`8a40fdc` el 07-04, `0914611` el run `cured` del 07-06). Ninguno es bloqueante de diseño; todos con causa raíz. Detalle en `docs_sesion/2026-07-04-cured-run-r2-human-gap.md`, `docs_sesion/2026-07-06-r2-human-fix-cured-validado.md` y `docs/12`.

| ID | Severidad | Localización | Descripción | Fix | Estado |
|---|---|---|---|---|---|
| C-01 | Media | main.py (`check_pod_health` / `_evaluate_rollback`) | **Falso rollback por restart benigno**: el health-check cuenta restarts sin mirar el motivo → un workload que reinicia por exit limpio (no crash) se lee como "no sano" → revierte un fix que curaba. Observado con el manifiesto chaos (`stress --timeout 60`) | Mirar `lastState.reason==OOMKilled` (el motivo real del último término), no solo el contador de restarts. Mitigación (quitar `--timeout` del chaos) ✅ confirmada en el run 07-06 (OOMKilled puro → healthy → `cured`) | ✅ DONE (2026-07-09: `check_pod_health` trae `lastState.terminated.reason` en el jsonpath; solo restarts con motivo en `_FAILURE_REASONS` marcan unhealthy, los benignos (`Completed`/vacío) se loggean y se ignoran; `observed_reasons` en `PodHealthStatus`) |
| C-02 | Media | remediation.py / main.py (approve path) | **Approve humano no siembra cooldown** → doble escalación misma causa raíz (OOM + CrashLoop del mismo pod) genera dos escalaciones. F-01 solo cubre la rama auto | Sembrar `acquire_workload_cooldown` también en el approve humano (paridad con auto) | ✅ DONE (2026-07-09: la rama approve siembra el cooldown tras ejecución con éxito de una remediación estructurada — al humano no se le gatea, solo se abre la ventana para el auto siguiente; fail-soft si Redis falla) |
| C-03 | Baja | config.py (`ESCALATION_TTL_MINUTES=60`) | Aprobar un mensaje MM de hace >60min → `get_escalation` None → "Escalación no encontrada o expirada". No es bug (nos pasó por aprobar 2h tarde) | Subir a 120-240min para demos/ausencias | ✅ DONE (ya estaba: `escalation_ttl_minutes=120` en Settings, sobreescribible por env `ESCALATION_TTL_MINUTES` — p.ej. 240 en el deployment para demos) |
| C-04 | Baja | scripts/chaos.sh | El ciclo de auto-cleanup (~300s) es más corto que el arco completo (patch → 300s ventana rollback → veredicto ≈10min) → borra el deployment a mitad → `NotFound` en captura/remediación/rollback | Documentado: **para validar el arco, aplicar el manifiesto a mano** (`kubectl apply`), no usar `scripts/chaos.sh` (sirve solo para medir MTTD/MTTR) | DOC (2026-07-04) |
| C-05 | Info | Ollama / infra | **MTTR dominado por el LLM (techo de hardware)**: qwen2.5:1.5b en CPU tarda 147-213s warm, timeoutea a 360s en cold. Ollama ya usa todo el nodo e2-standard-2 (2 vCPU, sin GPU) | No bajable con más CPU. MTTD=5s. Aceptado; un nodo con GPU lo resolvería | WONTFIX (sandbox) |
| C-06 | Alta | main.py (approve path) | **Gap R2-humano**: el veredicto `cured`/`rolled_back` de un fix **aprobado por un humano** NO alimentaba el bucle de aprendizaje R2 — `_reupsert_incident_outcome` tiene guarda `if not ctx.doc_id: return` y la rama approve llamaba a `_schedule_rollback_evaluation` sin `doc_id`. No es "2 líneas": `make_incident_doc_id` embebe `time.time()` → el doc_id no es reproducible en el approve, hay que acarrearlo por la escalación en Redis | Campo `incident_doc_id` en `PendingEscalation` (round-trip Redis, back-compat); la rama approve pasa `doc_id`+`remediation` a `_schedule_rollback_evaluation` (espejo del auto); ingest final reusa el mismo doc_id + `auto_pending` | ✅ DONE (`ca159be`, validado en cluster 07-06: `verdict_total{cured}=1.0`) |
| C-07 | Media | validation.py / remediation.py (seal de comandos free-text) | **Factibilidad ≠ seguridad**: la validation layer clasifica `kubectl top pod`/`top node` como SAFE (read-only), pero al ejecutar da `Forbidden` (la SA no tiene `metrics.k8s.io`; `top node` es cluster-scoped). Visto al aprobar una escalación HighCPU nocturna (camino free-text). No es bug de RBAC — es least-privilege + fail-honest | Pre-flight `kubectl auth can-i` al sellar comandos free-text → los no ejecutables se muestran como "comando sugerido (sin permisos)" en vez de ejecutar-y-fallar. **NO ampliar RBAC** (viola least-privilege + convención sin ClusterRoles) | DONE (2026-07-10): `remediation.py` `auth_can_i_args` (mapea la forma SAFE a verb/resource; `top pod`→`pods.metrics.k8s.io`, `top node`→`nodes.metrics.k8s.io` cluster-scoped) + `check_command_executable` (fail-open salvo "no" explícito) + `partition_by_permission`. `main.py`: la escalación free-text separa aprobables (botones) de denegados (sugeridos sin permiso); todo-denegado → notificación sin botones. El comando estructurado del motor NO pasa por el check (determinista). Solo ESCALATE. +~17 tests. NO se amplió RBAC |
| C-08 | Baja | remediation.py (escalación >2× → approve) | **La escalación por overshoot no ofrece el valor conservador**: cuando el LLM propone >2× (p.ej. 512Mi=16× sobre 32Mi), el motor escala con el valor del modelo intacto (correcto: no recorta a la callada). Pero al humano solo se le ofrece aprobar el 512Mi, sin el ×2 (64Mi) como alternativa por defecto. Surgió revisando el arco de la demo (2026-07-08): el diseño es correcto (≤2× = frontera de autonomía, no techo), pero un humano apurado podría querer el salto conservador de un clic | La escalación podría sugerir **dos** opciones: el valor del modelo y el ×2 determinista, dejando elegir. Refinamiento de UX de la escalación, no de seguridad (el motor ya impide el auto-16×) | ✅ DONE (2026-07-10, `588e3a9`): `structured_command_variants` en `remediation.py` — 2 botones `approve_engine` (×2 motor, ✅ recomendada, primera) / `approve_model` (valor LLM, ⚠️) cuando difieren, ambos sintetizados con `build_set_resources_command`; **cada botón firma su propia acción por HMAC** (un campo `variant` sin firmar sería flippable en tránsito); 1 variante legacy `approve` si coinciden o current no doblable (back-compat exacto, cero migración de escalaciones en vuelo); safety-net value-agnostic (el rollback revierte al snapshot, no a `new_value`). +8 tests |

---

## Incidente nocturno 13→14-jul + forense del auto-bucle (2026-07-14)

> La noche antes del chapter: TargetDown flapping sobre el propio agente (4 pods recreados 23:36–05:29) y un liveness-kill en cold start a las 08:47. El forense (RS history + managedFields + Cloud Logging, porque el TSDB de Prometheus se evaporó — ver F-27) destapó algo mayor: **el agente llevaba una semana auto-remediándose a sí mismo**. Cadena de spec del deployment: 384Mi→512Mi (07-04) →1Gi→2Gi (07-06) →4Gi→5Gi→10Gi (07-07) → revert a 2Gi. Smoking gun en Cloud Logging (07-07 06:00:23Z): `KubePodCrashLoopBackOff` sobre su propio pod → `grounded=1.0` (restart_count=3, **`last_state_reason="Error"` — NO OOMKilled**) → el LLM propuso 1Gi (bajar), el motor lo marcó inusable y sintetizó ×2=4Gi → `action=auto_remediate` → `kubectl set resources deployment agent ... --limits=memory=4Gi`, sin humano, a las 6 AM. Causa raíz del bucle: churn de nodos GKE (upgrades + preemption por pods system-critical tipo kube-dns, "Insufficient cpu" en nodos e2-standard-2 al 78%+ de requests) → cold starts → liveness-kill (F-19/F-20) → CrashLoop → auto-misdiagnóstico como memoria (F-26) → self-patch (F-21) → rollout → más restarts. Detalle: `docs_sesion/2026-07-14-incidente-liveness-coldstart.md`.

| ID | Severidad | Localización | Descripción | Fix | Estado |
|---|---|---|---|---|---|
| F-19 | Alta | k8s/deployment-agent.yaml | **Sin startupProbe**: cold start con deps caídas supera la ventana del liveness (~55s: initialDelay 10 + 3×15) → SIGKILL en mitad del lifespan → CrashLoop. Observado 08:47 (murió a los 77s, "Waiting for application startup", nunca abrió el puerto) | startupProbe `/healthz` period 10, failureThreshold 30 (300s), timeout 3 — desactiva liveness/readiness hasta que la app arranca. Solo `kubectl apply`, sin rebuild | TODO (hoy, MS-2) |
| F-20 | Alta | main.py:557-598 / rag.py:61 | **Startup no acotado**: `chromadb.HttpClient` y `redis.ping()` sin timeout — el fail-open del lifespan solo salta si la conexión FALLA; si cuelga (servicio sin endpoints con blackhole en vez de refused, DNS degradado tras churn) el lifespan nunca termina y uvicorn nunca abre :8000. Evidencia: 65s de silencio entre el log de telemetría chroma y el SIGKILL; con deps calientes el startup tarda 3s (log 07-07 06:00:50→53) | `asyncio.wait_for` ~15s al bloque chroma; `socket_connect_timeout=5` + `wait_for` al ping de Redis → lifespan acotado <45s determinista | TODO (código hoy MS-4, deploy post-chapter) |
| F-21 | Alta | remediation.py | **El motor auto-remedia su propio deployment** — ya NO es hipotético: ≥4 self-patches auto la semana del 07-04/07-07 (cadena ×2 hasta 10Gi). Bucle: self-patch → rollout → se mata a sí mismo → (posible) rollback recuperado por el pod nuevo → flapping. El cooldown F-01 limita frecuencia, no rompe el bucle. Anoche se salvó solo porque TargetDown no es estructurado | Regla self-target: si `pa.namespace/name` == el propio agente (settings `self_workload_*`) → forzar ESCALATE con `reason_code=self_target` (patrón 4.7); el humano puede seguir aprobando, solo se veta la rama auto. *El médico no se opera a sí mismo* | TODO (código hoy MS-4, deploy post-chapter) |
| F-22 | Media | k8s/deployment-agent.yaml | **Drift explicado**: manifiesto 384Mi vs vivo 2Gi. Actor = el propio motor (fieldManager `kubectl-set`, cadena de self-patches F-21; el 2Gi actual es un artefacto del bucle, no un right-sizing). Un apply hoy regresaría la memoria a 384Mi en el rollout | Reconciliar manifiesto a 2Gi hoy (headroom seguro para la demo); right-sizing real post-chapter con serie `container_memory_working_set_bytes` fresca | TODO (hoy, MS-2) |
| F-23 | ~~Media~~ N/A | k8s/deployment-agent.yaml | ~~Subir request de memoria contra evictions~~ **Revisado tras forense**: las muertes nocturnas fueron **preemptions por prioridad** (kube-dns prio 2000000000, "Insufficient cpu"), no evictions por presión de memoria (0 events Evicted/OOMKilling en Cloud Logging). Subir requests NO defiende contra preemption (las víctimas se eligen por priority) y agrava el "Insufficient cpu" | Sin acción hoy. PriorityClass requeriría recurso cluster-scoped (vetado por convención). Aceptar el riesgo residual + verificar estabilidad 30 min antes de grabar | REVISADO (no aplica) |
| F-24 | Baja | rag.py:61 (`get_chroma_client`) | Telemetría chroma (posthog) activa en cluster sin internet: ruido en logs (`Failed to send telemetry event ClientStartEvent: capture() takes 1 positional argument but 3 were given` — visto 07-07) y riesgo teórico de bloqueo | `chromadb.config.Settings(anonymized_telemetry=False)` en el HttpClient | TODO (post-chapter, junto a F-20) |
| F-25 | Baja | UX / deck | **"Confidence grounded" sobre-comunica**: el grounding avala que el target existe y que hay señales (restarts), no la historia causal. Evidencia dura: `grounded=1.0` el 07-07 con `last_state_reason="Error"` en el propio prompt mientras el sistema trataba un liveness-kill como problema de memoria — R4 Capa B (el 1.5b ignora las etiquetas) manifestándose en producción | Matiz en QA del guion (hoy MS-3); renombrar el label (p.ej. `target_grounded`) post-chapter | TODO (QA hoy) |
| F-26 | **Alta** | remediation.py (`is_structured_remediation` / gates auto) | **La rama auto-memoria no exige `last_state_reason=OOMKilled`**: cualquier crash loop con `restarts ≥ min_restarts` es elegible para el ×2 de memoria, aunque el motivo de terminación diga otra cosa (el snapshot YA trae `last_state_reason` y el gate no lo mira). Peor: si el valor del LLM es inusable (p.ej. proponía BAJAR a 1Gi), la síntesis ×2 escala recursos igualmente. Así se llegó a 10Gi contra un bug de infra | Gate: elegibilidad auto del campo memoria solo si `snapshot.last_state_reason == "OOMKilled"`; resto → ESCALATE (`reason_code=reason_mismatch`). Es la generalización de C-01 (que ya lo hace en el rollback) a la entrada del motor | TODO (candidato a MS-4 hoy — mismo área que F-21; si no, post-chapter P0) |
| F-27 | Media | k8s/prometheus.yaml | **Prometheus sin persistencia**: el pod se reprogramó con el churn y el TSDB se evaporó → la evidencia R5 (`aiops_incident_resolution_seconds{OOMKilled}=92.47s` del 07-13) ya no existe y el forense nocturno hubo que hacerlo por Cloud Logging. El screenshot Gate 8 "cuando quieras" era una ilusión: la ventana de captura es la vida del pod | Hoy: regenerar la serie con el arco del video y capturar Grafana EN el momento (Gate 8 en vivo). Post-chapter: PVC para Prometheus (patrón chromadb) o retention job | TODO (hoy en el arco del video) |

**Notas operativas del forense (no son findings de código):** los 2 nodos guaranteed fueron reemplazados HOY (12:37Z y 13:48Z, v1.36.0-gke.4447000) y los nuevos **ya no tienen el taint `guaranteed`** (sí la label) → pods de otros tenants pueden aterrizar y disputar CPU; el pod del agente fue recreado ~8 veces en 15h (todas preemption/churn, mismo RS). Riesgo residual de churn hoy: verificar 30 min de estabilidad antes de grabar.

---

## Notas

- Los `WONTFIX` reflejan decisiones conscientemente aceptadas para el MVP del TFG.
- Los findings de sesión #1 tienen tests añadidos que documentan el comportamiento correcto.
- La sesión #8 (k8s nodeSelector guaranteed + docs sync) no tiene findings en este backlog — es una tarea de infraestructura/docs, no de calidad de código. Sesiones #1-#8 completadas.
- X2: `backoff_delay` helper en `utils.py` consolida el cálculo de delay; wrapper completo no extraído porque las semánticas son incompatibles (mattermost retorna bool, /extract lanza HTTPException).
