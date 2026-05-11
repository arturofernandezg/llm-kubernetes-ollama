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
| R7 | medium | remediation.py:392-395 + 428-431 | `safe_commands` list comprehension duplicada 2× (idéntica en `build_remediation_result` y `process_remediation`) | Extraer helper `_get_safe_commands(validations)` | TODO |
| R8 | medium | remediation.py:252 | Regla 4.6: si `current_value` parsea a `0` bytes, `new_bytes > 2 * 0` es siempre True → cualquier cambio bloquea | Guard: si `current_bytes == 0`, devolver ESCALATE con `reason_code: zero_current_memory` | TODO |

---

## main.py — medium/low (pendiente sesión #3)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| M1 | medium | main.py:27 | `ActionCallbackContext` importado pero nunca referenciado directamente en main.py | Eliminar import | TODO |
| M2 | low | main.py:85 | `datetime.now()` naive en cleanup; si el host cambia TZ, el math de TTL deriva | Usar `datetime.now(timezone.utc)` consistente con el resto | TODO |
| M3 | medium | main.py:220-272 | `diagnosis["confidence"]` y `diagnosis["risk"]` accedidos directamente (KeyError si falta); contrasta con estilo `.get()` del resto | `diagnosis.get("confidence", 0.0)` / `diagnosis.get("risk", "high")` | TODO |
| M4 | medium | main.py:284-299 | `_format_escalation_body` accede `diagnosis['diagnosis']`, `diagnosis['confidence']`, `diagnosis['risk']` directamente | Mismo fix `.get()` defensivo | TODO |
| M5 | medium | main.py:226-232,277-281,396-399 | Extracción de `severity/pod/namespace/alert_name` duplicada 3× en formatters | Extraer `_extract_alert_meta(alert) -> tuple` helper | TODO |
| M6 | medium | main.py:491 | `else` de `action == "approve"` acepta cualquier valor como reject (typos, payloads malformados) | `elif action == "reject"` + 400 en acción desconocida | TODO |
| M7 | medium | main.py:494 | Excepción en `execute_commands` loguea el error pero no incrementa counter de fallo | `REMEDIATION_COUNTER.labels(action="human_approve_failed").inc()` | TODO |
| M8 | low | main.py:316 | `DIAGNOSIS_COUNTER.labels(outcome="rag_ok")` no está en la docstring de labels del counter (L52) | Añadir `rag_ok` a docstring o renombrar | TODO |
| M9 | low | main.py:497,507 | Labels `human_approved`/`human_rejected` en `REMEDIATION_COUNTER` no declarados en docstring (L57) | Actualizar docstring | TODO |
| M10 | medium | main.py:309 | Outer `try/except Exception` en `_process_alert_with_diagnosis` demasiado amplio; puede enmascarar bugs en el fallback mismo | Narrow scope al bloque de pipeline, no al fallback send | TODO |

---

## mattermost.py — medium/low (pendiente sesión #4)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| MM1 | medium | mattermost.py:18 | `_post_with_retry` crea nuevo `httpx.AsyncClient` por llamada — no reutiliza el cliente compartido de `app.state` | Aceptar parámetro `client` o reutilizar cliente compartido | TODO |
| MM2 | low | mattermost.py:20 | `hasattr(settings, 'mattermost_webhook_url')` es dead code: Settings de Pydantic siempre tiene el atributo | Eliminar check `hasattr`, dejar solo check de string vacío | TODO |
| MM3 | medium | mattermost.py:31,35,42,47,53 | f-strings en logger; inconsistente con `extra={}` estructurado del resto del proyecto | Convertir a `logger.info("...", extra={"attempt": ...})` | TODO |
| MM4 | medium | mattermost.py:41 | `except Exception: return False` swallows `TypeError`/`KeyError` de payload malformado — caller no puede distinguir error de config vs red | Narrow exception o re-raise inesperados | TODO |
| MM5 | medium | mattermost.py:72-115 | `send_escalation_with_buttons`: no validación de longitud de `attachment_text` (límite Mattermost 16KB) | Truncar `attachment_text` si supera umbral | TODO |
| MM6 | low | mattermost.py:72 | Parámetro `safe_commands` en `send_escalation_with_buttons` nunca se usa en el cuerpo de la función | Eliminar parámetro o usarlo para renderizar botones por comando | TODO |
| MM7 | low | mattermost.py:86-112 | Color `#FF6600` y labels de botones hardcodeados | Extraer como constantes de módulo | TODO |
| MM8 | low | mattermost.py:60-69 | `send_mattermost_alert`: no truncation guard para `message` muy largo (límite 16KB Mattermost) | Truncar con ellipsis si supera umbral | TODO |

---

## Cross-cutting (pendiente sesión #5)

| ID | Severidad | Localización | Descripción | Fix propuesto | Estado |
|---|---|---|---|---|---|
| X1 | medium | main.py:228-230,277-280,395-399 | Extracción `severity/pod/namespace/alert_name` de `AlertItem.labels` duplicada 3× | Helper `_extract_alert_meta(alert)` o método `AlertItem.meta()` en schemas.py | TODO |
| X2 | medium | mattermost.py:_post_with_retry / main.py:574-613 | Retry con exponential backoff implementado 2× con detalles divergentes | Helper `retry_with_backoff(coro, max_attempts, base, max_delay)` compartido | TODO |
| X3 | low | main.py + remediation.py vs mattermost.py | Logger style inconsistente: primeros usan `extra={...}`, mattermost.py usa f-strings | Estandarizar a `extra={...}` en todo el proyecto | TODO |
| X4 | high | main.py:80 | `PENDING_ESCALATIONS` perdido en restart de pod — decisiones humanas pendientes desaparecen silenciosamente | Documentar limitación explícitamente; futura mejora: persistir en ChromaDB o Redis | WONTFIX (MVP) |
| X5 | medium | main.py:488 | Callback de Mattermost no verifica HMAC/shared secret — cualquiera que conozca el endpoint puede aprobar remediaciones | Añadir verificación de token en callback URL | TODO |

---

## Notas

- Los `WONTFIX` reflejan decisiones conscientemente aceptadas para el MVP del TFG.
- Los findings de sesión #1 tienen tests añadidos que documentan el comportamiento correcto.
- La sesión #6 (k8s nodeSelector guaranteed) no sale de este backlog — es una tarea de infraestructura, no de calidad de código.
