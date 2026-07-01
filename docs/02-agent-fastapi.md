# Agente FastAPI — Core API (Remediación AIOps)

> [!NOTE]
> El agente está en transición de Fase 0 (extracción NLP → Terraform) a Fase 1-3
> (alertas → RAG → diagnóstico → remediación). Los endpoints y módulos legacy se conservan
> pero no reciben desarrollo activo.

## Estructura modular (`agent/`)

Versión actual: 0.6.0

| Módulo | Responsabilidad | Fase |
|---|---|---|
| `main.py` | FastAPI app, endpoints, lifespan (arranca consumer + reclaim de la cola), middleware de logging, retry logic, productor de la cola en el webhook | 0+ |
| `config.py` | Pydantic BaseSettings, setup de logging JSON estructurado, `redis_host/redis_port`, settings de cola `queue_*` | 0+ |
| `streams.py` | Cola Redis Streams (F2): `enqueue_alert` (dedup SETNX + `XADD MAXLEN ~`, fail-closed), `ensure_group(start_id)`, `consume_loop` (XREADGROUP 1-a-1 in-process; **self-healing ante NOGROUP**), `reclaim_pending` (XPENDING+XCLAIM+dead-letter, fail-soft), `_ack_and_process`, `consumer_name()`. Métricas `aiops_queue_*` | F2 |
| `schemas.py` | Modelos Pydantic v2 (alertas, extracción, diagnóstico) | 0+ |
| `extraction.py` | PROMPT_TEMPLATE, `extract_json()` con 3 estrategias de fallback | 0 (legacy) |
| `validation.py` | `validate_params()` — validación no-bloqueante contra valores GCP | 0 (legacy) |
| `tf_generator.py` | `safe_name()`, `generate_terraform()`, template Terraform | 0 (legacy) |
| `mattermost.py` | Cliente HTTP async para Mattermost con retry/backoff | 1 |
| `rag.py` | Cliente ChromaDB, ingesta (runbooks + incidents), query con embedding, construcción de queries enriquecidas | 2 |
| `diagnosis.py` | Prompt AIOps contextual, `generate_diagnosis()`, parsing JSON estructurado del LLM, `_clamp()` | 2 |
| `remediation.py` | Validation layer (classify/validate commands), decision engine (9 reglas cascada), executor dual-mode + rollback | 3 |
| `escalation_store.py` | Store/get/delete/count async de escalaciones sobre `redis.asyncio`. Serializa a JSON con TTL nativo. Fail-open. | Mini-Fase 4 |
| `utils.py` | `backoff_delay()` helper (exponential backoff compartido) | 3 |

## Endpoints

| Método | Path | Descripción | Dependencias | Fase |
|---|---|---|---|---|
| GET | `/healthz` | Liveness probe. Siempre 200. | Ninguna | 0 |
| GET | `/readyz` | Readiness probe. 200 si **Redis** alcanzable (la cola es la dependencia de ingesta; Ollama lento/caído no saca al pod). 503 si Redis no responde. | Redis | F2 |
| POST | `/webhook/alert` | Ingesta alertas Alertmanager → **encola en Redis Streams** (firing; fail-closed 503 si Redis cae) o notifica directo (resolved). El consumidor in-process corre el pipeline RAG→diagnóstico→remediación→Mattermost | Redis (ingesta); Ollama/ChromaDB/Mattermost (consumidor) | 1-3, F2 |
| POST | `/webhook/action` | Callback de botones interactivos Mattermost (Aprobar/Rechazar escalaciones) | ChromaDB, Mattermost | 3 |
| POST | `/webhook/command` | Slash command `/aiops` (status / incidents / help). Auth: `MM_COMMAND_TOKEN` static token. Responde ephemeral. | Ollama, ChromaDB | Mini-Fase 4 |
| POST | `/extract` | **(Legado)** Extracción de parámetros desde texto a JSON. | Ollama | 0 |
| GET | `/metrics` | Métricas Prometheus (auto-instrumentado + contadores custom). | Ninguna | 0 |

## Flujo de /webhook/alert (evolución por fases)

> **F2 (actual)**: el webhook ya **no** procesa inline. Para cada alerta `firing` **encola** en el stream `aiops:alerts` (`streams.enqueue_alert`: dedup SETNX por fingerprint `alertname:ns:pod` + `XADD MAXLEN ~`) y responde 200 de inmediato (desacople del LLM lento). **Fail-closed**: si Redis no está o el XADD falla → 503 y Alertmanager reintenta (no se pierde la alerta). Las `resolved` siguen como notificación directa a Mattermost (BackgroundTask). Un consumidor in-process (`consume_loop` en el lifespan) drena el stream 1 a 1 e invoca `_handle_stream_entry` → `_process_alert_with_diagnosis` (los pasos de abajo, sin cambios). Durabilidad: `reclaim_pending` reprocesa entradas del PEL tras un reinicio; poison messages (>`queue_max_deliveries`) van a `aiops:alerts:dead`. Ver `docs/07` §F2 y `agent/streams.py`.

> **Self-heal del consumidor ante `NOGROUP`**: si Redis se recrea bajo un agente vivo (Redis sin PVC: un bump de recursos recrea el pod → desaparecen stream + consumer group), el `XREADGROUP` empieza a fallar con `NOGROUP`. El `except` de `consume_loop` **no** hace `continue` inmediato (eso era un busy-spin de cientos de iter/s que saturaba la CPU de Redis y disparaba `HighCPU`). En su lugar: incrementa un contador local de fallos consecutivos (reset a 0 tras un éxito), y si el error es `NOGROUP` recrea el grupo con `ensure_group(start_id="$")` antes de un `asyncio.sleep(backoff_delay(...))` exponencial acotado (mismo helper que `main.py`/`mattermost.py`). Se elige `id="$"` (no `"0"`): `$` se salta el gap de entradas durante el hueco —recuperable porque Alertmanager reenvía las firing (`repeat_interval`)— mientras que `id="0"` re-entregaría TODO el historial retenido (`MAXLEN ~1000`) **sin pasar por `enqueue_alert`** → la dedup-key no lo frenaría → replay masivo de diagnósticos/remediaciones sobre estados ya resueltos. `ensure_group` tiene `start_id="0"` por defecto (arranque del lifespan, stream vacío → inocuo); solo el self-heal usa `"$"`.

**Fase 1 (legacy del pipeline)**: Recibe payload → log estructurado → formatea mensaje → envía a Mattermost (BackgroundTask).

**Fase 2 (planificado)**:
1. Recibe payload Alertmanager (validado por `AlertmanagerPayload`).
2. Normaliza: extrae `alertname`, `pod`, `namespace`, `severity`, `description`.
3. Construye query enriquecida para ChromaDB (no solo log raw — incluye labels + features).
4. Genera embedding via Ollama (`nomic-embed-text`).
5. Query ChromaDB: busca top-3 en `runbooks` + top-2 en `incidents`.
6. Construye prompt contextual: alerta + documentos relevantes + instrucción de output JSON.
7. LLM genera `{ diagnosis, commands[], confidence, risk, explanation }`.
8. Envía a Mattermost: mensaje enriquecido con diagnóstico, comandos sugeridos, risk level.

**Fase 3 (implementado)**:
9. Validation layer evalúa commands contra whitelist/blacklist.
10. Motor de decisión (9 reglas): `AUTO_REMEDIATE` | `ESCALATE` | `SUGGEST_ONLY`.
11. Si `ESCALATE` con `safe_commands` no vacío → mensaje Mattermost con botones `[✅ Ejecutar]` / `[❌ Rechazar]` (via `send_escalation_with_buttons`). El `incident_id` y payload se persisten en **Redis** via `escalation_store.store_escalation()` (TTL 60 min). Si Redis no está disponible → mensaje degradado sin botones (fail-open).
12. Si `AUTO_REMEDIATE` → ejecuta kubectl directamente (respeta `REMEDIATION_DRY_RUN`).
13. Resultado (aprobado/rechazado/auto) se persiste en colección `incidents` de ChromaDB.

**Mini-Fase 4 Sesión #5 (rollback automático)**:
14. Si `AUTO_REMEDIATE` ejecuta al menos un comando exitoso y hay `pre_patch_snapshot` capturado, se registra un `RollbackContext` en `IN_FLIGHT_ROLLBACKS` (in-memory, `asyncio.Lock`) y se lanza `asyncio.create_task(_evaluate_rollback(incident_id))`.
15. Tras `REMEDIATION_ROLLBACK_TIMEOUT` segundos (default 300), `_evaluate_rollback` consulta el estado de los pods con `kubectl get pods -n <ns> -l <selector>`.
16. Si **todos** los pods están `Running` con `restartCount==0` → counter `aiops_remediation_rollback_total{outcome=healthy}`, mensaje Mattermost "Remediation healthy".
17. Si algún pod está en estado fallido o ha reiniciado → ejecuta `kubectl set resources deployment <name> -n <ns> --containers=<container> --limits=<recurso>=<pre_patch_value>` (respeta `DRY_RUN`). El `<recurso>` (`cpu`|`memory`) lo deriva `_limit_resource(snapshot.field)` — field-agnostic desde F3 (antes hardcodeaba `memory`). Counter `outcome=reverted` o `outcome=revert_failed`. Mensaje Mattermost con resultado.

**Diagrama de flujo de rollback**:
```
AUTO_REMEDIATE + snapshot capturado
    → IN_FLIGHT_ROLLBACKS[incident_id] = RollbackContext
    → asyncio.create_task(_evaluate_rollback)
    → sleep(300s)
    → check_pod_health(selector) — kubectl get pods
        ├── healthy=True  → counter=healthy, MM "Remediation healthy"
        └── healthy=False → revert_patch(pre_patch_value)
                               ├── success → counter=reverted, MM "Rollback executed"
                               └── failure → counter=revert_failed, MM "Rollback FAILED"
```

**Rollback no se ejecuta si**:
- `REMEDIATION_ROLLBACK_ENABLED=false`
- Acción no es `AUTO_REMEDIATE`
- Ningún comando del patch fue exitoso (`execute_results` vacío o todos fallidos)
- `proposed_action` ausente en diagnosis → snapshot no capturado (`counter=skipped_no_snapshot`)
- `DRY_RUN=true` → `check_pod_health` devuelve `healthy=True` siempre (no hay patch real que revertir)

### /webhook/action — Callback de botones

Mattermost POST cuando el operador pulsa un botón de acción:

```json
{
  "user_name": "arturo",
  "context": {
    "action": "approve",
    "incident_id": "<uuid>",
    "hmac_token": "<sha256-hex>"
  }
}
```

`hmac_token` = `HMAC-SHA256(incident_id:action, WEBHOOK_SECRET)`. Si `WEBHOOK_SECRET` está vacío (dev/test), el campo es `null` y la verificación se omite.

| Campo `action` | Comportamiento | Métrica |
|---|---|---|
| `"approve"` | Llama `execute_commands(safe_commands)`, persiste en ChromaDB | `human_approved` |
| `"reject"` | Persiste decisión, no ejecuta comandos | `human_rejected` |

Respuesta JSON que Mattermost usa para actualizar el mensaje original y eliminar los botones:

```json
{
  "update": {
    "message": "✅ Remediación ejecutada por @arturo\n```\n[DRY-RUN]...\n```",
    "props": {"attachments": []}
  }
}
```

Si el `incident_id` no existe (desconocido o expirado tras 60 min) → responde con `ephemeral_text` visible solo al operador, sin ejecutar nada.

## Mejoras Mini-Fase 4 (2026-05-27)

### Dedup cluster-wide en la cola (F2 — reemplaza el dedup in-flight)

> El dedup in-memory original (`IN_FLIGHT_ALERTS` + `_INFLIGHT_LOCK`) **fue retirado** al hornear F2. El dedup vive ahora en Redis, dentro de `enqueue_alert`, y es cluster-wide (sobrevive réplicas y reinicios).

En `enqueue_alert`, para cada alerta `firing`:
- Fingerprint = `alertname:namespace:pod`.
- `SET aiops:seen:<fp> "1" NX EX <dedup_window_seconds>` (default 300s). Si la clave ya existe (reenvío de Alertmanager u otro pod dentro de la ventana) → `enqueue_alert` devuelve `None`, el webhook incrementa `aiops_dedup_skipped_total{alertname}` y **no** encola.
- Si es nueva → `XADD` al stream y devuelve el id.

Suprime reenvíos de Alertmanager y suaviza los duplicados del replay (at-least-once). Reutiliza el counter `aiops_dedup_skipped_total`.

### Mensajes claros en timeout LLM

En el `except` de diagnóstico (`_process_alert_with_diagnosis`), `httpx.TimeoutException` se distingue de errores genéricos:
- Timeout → flag `_llm_timeout = True`
- `_format_diagnosis_message(llm_timeout=True)` → mensaje Mattermost: `"⚠️ Diagnóstico no disponible: el LLM agotó el tiempo (HTTP_TIMEOUT=Xs)."` + alerta cruda
- Fallo genérico → `"⚠️ Diagnóstico no disponible (error interno)."` + alerta cruda

### Persistencia de escalaciones en Redis

`PENDING_ESCALATIONS` dict in-memory eliminado. Reemplazado por `escalation_store.py` + `app.state.redis`:

| Función | Acción |
|---|---|
| `store_escalation(id, payload, ttl, redis)` | `SET escalation:{id} <json> EX ttl` |
| `get_escalation(id, redis)` | `GET escalation:{id}` → dict o None |
| `delete_escalation(id, redis)` | `DEL escalation:{id}` |
| `count_escalations(redis)` | `SCAN MATCH escalation:*` → count |

Redis init en `lifespan`: `aioredis.Redis(host, port, decode_responses=True)` + `ping()`. Fail-open: si ping falla → `app.state.redis = None`, warning en startup. `_cleanup_expired_escalations()` eliminado (Redis TTL nativo lo gestiona). Helpers de serialización `_escalation_to_dict` / `_dict_to_escalation` en `main.py` (evitan circular import con `PendingEscalation`).

## Motor de remediación — dimensión CPU + re-sourcing (F3)

### Dimensión CPU (field-agnostic)

El motor era memory-only; F3 lo generaliza a `resources.limits.cpu` sin duplicar lógica:
- `parse_cpu_to_millicores(value)` (`"250m"→250`, `"1"→1000`, `"0.5"→500`, cores×1000) análogo a `parse_memory_to_bytes`.
- Tabla `_LIMIT_FIELD_PARSERS` (`resources.limits.memory`→bytes, `resources.limits.cpu`→millicores) y helper `_limit_resource(field)` → `"cpu"|"memory"` (default `memory` para campos vacíos/desconocidos — fail-safe hacia el caso histórico).
- **Regla 4.6** (cap ≤2×) hace dispatch por `field`: parser según el recurso, reason codes por-recurso (`{cpu,memory}_exceeds_2x`, `missing_{...}_value`, `zero_current_{...}`, `unparseable_{...}`).
- `capture_pre_patch_value` (jsonpath `{.resources.limits.<resource>}`) y `revert_patch` (`--limits=<resource>=...`) derivan el recurso de `field`/`snapshot.field`.
- **Mecanismo elegido**: bump vertical del limit, NO scale/HPA (reusa snapshot/health/rollback del camino de memoria; HPA requiere `metrics-server`, sin confirmar en GKE). Auto-CPU tras flag `REMEDIATION_AUTO_CPU_ENABLED` (default off, escalate-first).

### Re-sourcing de decisiones ("el modelo propone, el motor dispone")

Causa raíz de por qué el auto **nunca disparaba**: `process_remediation` ejecutaba en AUTO los comandos de `diagnosis["commands"]` (el modelo pone ahí *investigativos*: `describe/top/logs` — `has_set_resources=null` en 4/5), no la `proposed_action` estructurada (que el modelo acierta 5/5). Fix:
- **`is_structured_remediation(diagnosis)`** — fuente única de elegibilidad: field elegible (memory siempre; cpu iff flag) + `name/ns/container` presentes + `namespace` en el allow-list `arturo-` + `current/new` parseables + `current>0` + **solo-subir** (`new>current`). NO comprueba ≤2× (eso lo dueña la regla 4.6).
- **`build_set_resources_command(deployment, namespace, container, field, value)`** — constructor puro y determinista del `kubectl set resources`; `revert_patch` delega en él. Cuando AUTO + estructurado, el motor sintetiza y ejecuta **solo ese** comando (no los investigativos).
- **Bypass de la regla 5 (riesgo)** si `is_structured_remediation`: el `risk` auto-rating del modelo lo sustituye el bound determinista (field elegible + solo-subir + ≤2× + reversible con health-check). **Es un upgrade de seguridad, no una rebaja**: ejecutar el comando sintetizado por el motor es más seguro que ejecutar el string de texto libre de un 1.5b. La **regla 6 (confianza ≥0.8)** se mantiene para todos.
- **Guardrail namespace allow-list** (`REMEDIATION_AUTO_NAMESPACE_PREFIX="arturo-"`): no auto cross-tenant en cluster compartido.

> **Límite conocido (slice C pendiente)**: el comando sintetizado toma `name/namespace/container/current/new` **directos de `proposed_action` (el LLM los emite)**. `is_structured_remediation` valida que existan y sean parseables, pero **NO que el deployment exista ni que `container` no sea un pod**. Un 1.5b los alucina (target=namespace, container=pod, current=lista fabricada) → `NotFound`/`unparseable` en cluster. temp=0 mató la varianza del *valor*; el *target* es problema de **sourcing** → slice C lo sella desde los labels de la alerta (namespace/container; deployment por strip-hash del pod) + gate de existencia + valores desde el snapshot, reduciendo el LLM a *field + dirección*.

## Flujo de /extract

1. Recibe `{"message": "texto en lenguaje natural"}` (validado por Pydantic, max 2000 chars)
2. Construye prompt con `PROMPT_TEMPLATE` + delimitadores `<user_message>...</user_message>`
3. Envía al LLM via cliente httpx compartido (`app.state.http_client`) con retry automático
4. Intenta extraer JSON de la respuesta con 3 estrategias (en `extraction.py`):
   - **direct**: parseo directo (el LLM devolvió JSON puro)
   - **markdown_block**: JSON dentro de ```json ... ```
   - **regex_search**: bracket counting — busca `{`, cuenta profundidad de llaves hasta encontrar `}` correspondiente
5. Valida parámetros extraídos contra valores conocidos GCP (en `validation.py`)
6. Devuelve respuesta estructurada con warnings, método de extracción, duración

## Schemas

### Alertmanager (Fase 1+)

```
AlertItem:
  status:       str ("firing" | "resolved")
  labels:       dict[str, str]    ← alertname, pod, namespace, severity
  annotations:  dict[str, str]    ← description, summary
  startsAt:     str
  endsAt:       str | None
  generatorURL: str | None
  fingerprint:  str | None

AlertmanagerPayload:
  receiver:          str
  status:            str ("firing" | "resolved")
  alerts:            list[AlertItem]
  groupLabels:       dict[str, str]
  commonLabels:      dict[str, str]
  commonAnnotations: dict[str, str]
  externalURL:       str | None
  version:           str | None
  groupKey:          str | None
```

### Diagnóstico AIOps (Fase 2 — implementado)

```
DiagnosisResponse:
  alert_id:     str
  diagnosis:    str              ← explicación del problema
  commands:     list[str]        ← comandos kubectl sugeridos
  confidence:   float (0.0-1.0)  ← confianza del LLM en su diagnóstico
  risk:         str              ← "low" | "medium" | "high"
  explanation:  str              ← razonamiento detallado
  rag_sources:  list[str]        ← IDs de documentos ChromaDB usados como contexto
  model_used:   str
  duration_ms:  int
```

### Human Escalation Callback (Fase 3)

```
ActionCallbackContext:
  action:      str          — "approve" | "reject"
  incident_id: str          — UUID que referencia PENDING_ESCALATIONS
  hmac_token:  str | None   — HMAC-SHA256(incident_id:action, WEBHOOK_SECRET); None cuando secret vacío
```

### Extracción Legacy (Fase 0)

```
InfraRequest:
  message: str (1-2000 chars, no vacío)

ExtractedParams:
  project_name:  str | None
  region:        str | None
  instance_type: str | None
  purpose:       str | None

ExtractResponse:
  request_id, input_message, extracted_parameters,
  validation_warnings, raw_response, model_used,
  extraction_method, duration_ms
```

## Validaciones GCP

- **Regiones permitidas** (en `VALID_REGIONS`): europe-west1/2/3/4, europe-southwest1, us-central1,
  us-east1, us-west1, asia-east1, asia-northeast1
- **Prefijos de instancia válidos**: e2-, n1-, n2-, n2d-, c2-, m1-, t2d-
- **Campos obligatorios**: project_name, region, instance_type, purpose
- No bloquea: genera warnings informativos
- **Nota**: la convención del proyecto MasOrange es solo europe-\*, pero `VALID_REGIONS`
  incluye también regiones US y Asia. Esto significa que `us-east1` pasa validación
  sin warning. Gap identificado el 2026-03-18 durante pruebas end-to-end.

## Variables de entorno

| Variable | Default | Descripción | Fase |
|---|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint de generación | 0+ |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint de modelos | 0+ |
| `OLLAMA_MODEL` | `tinyllama` | Modelo generativo (en K8s: `qwen2.5:1.5b`) | 0+ |
| `OLLAMA_TEMPERATURE` | `0.0` | Temperatura de generación (greedy/determinista; viaja en `options.temperature` del POST a Ollama). Un motor de remediación necesita razonamiento reproducible — mató la varianza run-to-run del `new_value` | F3 |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings (768 dims) | 2 |
| `OLLAMA_EMBED_URL` | `http://ollama-svc:11434/api/embeddings` | Endpoint de embeddings | 2 |
| `CHROMADB_HOST` | `chromadb-svc` | Host de ChromaDB | 2 |
| `CHROMADB_PORT` | `8000` | Puerto de ChromaDB | 2 |
| `MATTERMOST_WEBHOOK_URL` | `None` | URL del webhook entrante de Mattermost | 1 |
| `WEBHOOK_SECRET` | `""` | Secreto HMAC-SHA256 para verificar callbacks de botones Mattermost. Vacío = sin verificación (dev/test). En K8s via Secret `agent-secrets.webhook-secret` (`optional: true`) | 3 |
| `REMEDIATION_AUTO_CPU_ENABLED` | `false` | Gate: extiende la excepción 4.5 (auto-remediación de `set resources`) a `resources.limits.cpu`. Off = escalate-first para CPU hasta validar en cluster; memoria siempre elegible (tutor-approved) | F3 |
| `REMEDIATION_AUTO_NAMESPACE_PREFIX` | `arturo-` | Guardrail blast-radius: `is_structured_remediation` rechaza `proposed_action` cuyo `namespace` no empiece por el prefijo → no auto cross-tenant en cluster compartido. `""` = sin restricción | F3 |
| `REMEDIATION_ROLLBACK_ENABLED` | `true` | Activa el mecanismo de rollback automático post-patch | Mini-Fase 4 |
| `REMEDIATION_ROLLBACK_TIMEOUT` | `300` | Segundos de espera antes de evaluar salud del pod tras un patch | Mini-Fase 4 |
| `REMEDIATION_ROLLBACK_GRACE` | `30` | Segundos de gracia para el rollout antes del health check (reservado, no usado aún en polling) | Mini-Fase 4 |
| `HTTP_TIMEOUT` | `300.0` | Timeout general del cliente HTTP (segundos). Default alineado a producción (PR-01); cubre el peor caso de diagnóstico (~252s) | 0+ |
| `HEALTH_TIMEOUT` | `5.0` | Timeout para health checks (segundos), incl. el `ping` de Redis en `/readyz` | 0+ |
| `QUEUE_STREAM_KEY` | `aiops:alerts` | Stream Redis donde encola el webhook | F2 |
| `QUEUE_GROUP` | `aiops-workers` | Consumer group del stream | F2 |
| `DEDUP_WINDOW_SECONDS` | `300` | TTL de la dedup-key por fingerprint (`aiops:seen:<fp>`) | F2 |
| `QUEUE_MAXLEN` | `1000` | `XADD MAXLEN ~` (acota la memoria del stream en Redis) | F2 |
| `QUEUE_MAX_DELIVERIES` | `3` | Entregas antes de mandar la entrada a dead-letter | F2 |
| `QUEUE_RECLAIM_INTERVAL_SECONDS` | `60` | Cada cuánto corre el reclaim periódico | F2 |
| `QUEUE_MIN_IDLE_SECONDS` | `600` | Idle mínimo para reclamar una entrada del PEL (debe superar el peor diagnóstico ~252s para no robar trabajo en curso) | F2 |
| `QUEUE_DEAD_LETTER_KEY` | `aiops:alerts:dead` | Stream de cuarentena para poison messages | F2 |
| `RETRY_MAX_ATTEMPTS` | `3` | Intentos máximos de retry hacia Ollama | 0+ |
| `RETRY_BASE_DELAY` | `1.0` | Delay base (segundos) para backoff exponencial | 0+ |
| `RETRY_MAX_DELAY` | `10.0` | Delay máximo (segundos) entre reintentos | 0+ |
| `LOG_LEVEL` | `INFO` | Nivel de logging (DEBUG, INFO, WARNING, ERROR) | 0+ |

## Cliente HTTP compartido

Creado en el lifespan de FastAPI, cerrado al apagar:
- Timeout de 120s para inferencia (POST /extract)
- Timeout de 5-10s para health checks (GET /readyz, /health)
- Reutiliza pool de conexiones TCP (no crea uno por request)

## Retry con exponential backoff

Implementado en `main.py` (commit 07ad2e3). Solo se aplica al endpoint `/extract`:

- **Errores transitorios** (se reintenta): `TimeoutException`, `ConnectError`
- **Errores permanentes** (NO se reintenta): `HTTPStatusError` (4xx/5xx de Ollama)
- **Fórmula de backoff**: `delay = min(base_delay * 2^attempt, max_delay)`
- **Default**: hasta 3 intentos, delays de 1s, 2s (capped a 10s)
- **Prometheus counter**: `aiops_ollama_retries_total{outcome}` — "success" o "exhausted"

Cuando se agotan los reintentos:
- `TimeoutException` → HTTP 504 ("LLM timeout")
- `ConnectError` → HTTP 502 ("LLM unavailable")

## Métricas Prometheus

Endpoint `GET /metrics` expuesto via `prometheus-fastapi-instrumentator`:

**Auto-instrumentación** (todos los endpoints):
- `http_requests_total{handler, method, status}` — contador de requests por endpoint
- `http_request_duration_seconds{handler, method}` — histograma de latencia por endpoint
- `http_request_duration_highr_seconds` — histograma de alta resolución (todos los endpoints)
- `http_request_size_bytes{handler}` — tamaño de requests entrantes

**Contadores custom**:
- `aiops_ollama_retries_total{outcome}` — resultado del retry ("success" / "exhausted")
- `aiops_extraction_total{method}` — método de extracción usado ("direct" / "markdown_block" / "regex_search" / "failed")
- `aiops_diagnosis_total{outcome}` — etapas del pipeline de diagnóstico (`rag_ok` / `rag_reconnect` / `rag_failed` / `success` / `llm_timeout` / `llm_error` / `pipeline_failed`). `rag_reconnect` = ChromaDB caché stale recuperado en caliente (PR-05); `llm_timeout` vs `llm_error` diferenciados (PR-06)
- `aiops_escalation_store_total{outcome}` — persistencia de escalaciones en Redis (`stored` = con botones / `redis_down` = degradado sin botones). Incrementa de forma visible durante el chaos de Redis (PR-06)

- `aiops_remediation_rollback_total{outcome}` — resultado del rollback post-patch (`scheduled` / `skipped_no_snapshot` / `healthy` / `reverted` / `revert_failed` / `evaluation_error`)

**Contadores de la cola (F2, definidos en `streams.py`)**:
- `aiops_queue_enqueued_total` — alertas encoladas (`XADD` exitoso); no incrementa en dedup
- `aiops_queue_processed_total{outcome}` — entradas procesadas por el consumidor (`success` / `error`)
- `aiops_queue_reclaimed_total` — entradas del PEL reprocesadas por `reclaim_pending` (replay)
- `aiops_queue_dead_total` — poison messages enviados a `aiops:alerts:dead` (>`queue_max_deliveries`)
- `aiops_queue_depth` — Gauge: entradas pendientes en el PEL (de `XPENDING` summary), actualizado en cada reclaim

> **Ojo**: los counters de Prometheus son **por-proceso** — tras reiniciar el pod, `aiops_queue_enqueued_total` vuelve a 0 aunque el stream conserve datos. El dato durable vive en Redis (`XLEN`/`XPENDING`), no en el counter.

**Datos reales observados** (2026-03-18, pod con ~40 min de uptime):
- `/healthz` latencia media: ~1.8ms (puro in-memory, sin dependencias)
- `/readyz` latencia media: ~62ms (consulta Ollama via red interna)
- `/extract` latencia: 7-45 segundos (inferencia LLM en CPU)
- `/readyz` 5xx: 23 requests durante arranque de Ollama (readiness probe correctamente devolviendo 503)

**Gap conocido — buckets del histograma para /extract**:
Los buckets por defecto del instrumentador llegan hasta 1s (0.1, 0.5, 1.0, +Inf).
Las inferencias LLM tardan 7-45s, así que todas caen en `+Inf` sin resolución intermedia.
Para Fase 2 se deberían añadir buckets en 5s, 10s, 30s, 60s, 120s.

## Logging JSON estructurado

Configurado en `config.py` con `python-json-logger`:

- Todas las líneas de log salen como JSON (compatible con Cloud Logging, ELK, Loki)
- Campos: `timestamp`, `severity`, `name`, `message`
- Middleware añade extras por request: `method`, `path`, `status`, `duration_ms`, `request_id`
- Nivel configurable via `LOG_LEVEL` (env var)

## Manejo de errores

| Error | HTTP | Detalle |
|---|---|---|
| Ollama timeout (120s) | 504 | "LLM timeout — model took too long" |
| Ollama HTTP error | 502 | "LLM returned error: {status_code}" |
| Ollama connection error | 502 | "LLM unavailable: {error}" |
| Input vacío/largo | 422 | Validación Pydantic |
