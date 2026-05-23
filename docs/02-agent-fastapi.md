# Agente FastAPI — Core API (Remediación AIOps)

> [!NOTE]
> El agente está en transición de Fase 0 (extracción NLP → Terraform) a Fase 1-3
> (alertas → RAG → diagnóstico → remediación). Los endpoints y módulos legacy se conservan
> pero no reciben desarrollo activo.

## Estructura modular (`agent/`)

Versión actual: 0.5.0

| Módulo | Responsabilidad | Fase |
|---|---|---|
| `main.py` | FastAPI app, endpoints, lifespan, middleware de logging, retry logic | 0+ |
| `config.py` | Pydantic BaseSettings, setup de logging JSON estructurado | 0+ |
| `schemas.py` | Modelos Pydantic v2 (alertas, extracción, diagnóstico) | 0+ |
| `extraction.py` | PROMPT_TEMPLATE, `extract_json()` con 3 estrategias de fallback | 0 (legacy) |
| `validation.py` | `validate_params()` — validación no-bloqueante contra valores GCP | 0 (legacy) |
| `tf_generator.py` | `safe_name()`, `generate_terraform()`, template Terraform | 0 (legacy) |
| `mattermost.py` | Cliente HTTP async para Mattermost con retry/backoff | 1 |
| `rag.py` | Cliente ChromaDB, ingesta (runbooks + incidents), query con embedding, construcción de queries enriquecidas | 2 |
| `diagnosis.py` | Prompt AIOps contextual, `generate_diagnosis()`, parsing JSON estructurado del LLM, `_clamp()` | 2 |
| `remediation.py` | Validation layer (classify/validate commands), decision engine (7 reglas cascada), executor stub (dry-run) | 3 |

## Endpoints

| Método | Path | Descripción | Dependencias | Fase |
|---|---|---|---|---|
| GET | `/healthz` | Liveness probe. Siempre 200. | Ninguna | 0 |
| GET | `/readyz` | Readiness probe. 200 si Ollama + modelo OK. | Ollama | 0 |
| POST | `/webhook/alert` | Ingesta alertas Alertmanager → normaliza → RAG → diagnóstico → Mattermost | Ollama, ChromaDB, Mattermost | 1-3 |
| POST | `/webhook/action` | Callback de botones interactivos Mattermost (Aprobar/Rechazar escalaciones) | ChromaDB, Mattermost | 3 |
| POST | `/webhook/command` | Slash command `/aiops` (status / incidents / help). Auth: `MM_COMMAND_TOKEN` static token. Responde ephemeral. | Ollama, ChromaDB | Mini-Fase 4 |
| POST | `/extract` | **(Legado)** Extracción de parámetros desde texto a JSON. | Ollama | 0 |
| GET | `/metrics` | Métricas Prometheus (auto-instrumentado + contadores custom). | Ninguna | 0 |

## Flujo de /webhook/alert (evolución por fases)

**Fase 1 (actual)**: Recibe payload → log estructurado → formatea mensaje → envía a Mattermost (BackgroundTask).

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
11. Si `ESCALATE` con `safe_commands` no vacío → mensaje Mattermost con botones `[✅ Ejecutar]` / `[❌ Rechazar]` (via `send_escalation_with_buttons`). El `incident_id` se guarda en `PENDING_ESCALATIONS` (in-memory, TTL 60 min).
12. Si `AUTO_REMEDIATE` → ejecuta kubectl directamente (respeta `REMEDIATION_DRY_RUN`).
13. Resultado (aprobado/rechazado/auto) se persiste en colección `incidents` de ChromaDB.

**Mini-Fase 4 Sesión #5 (rollback automático)**:
14. Si `AUTO_REMEDIATE` ejecuta al menos un comando exitoso y hay `pre_patch_snapshot` capturado, se registra un `RollbackContext` en `IN_FLIGHT_ROLLBACKS` (in-memory, `asyncio.Lock`) y se lanza `asyncio.create_task(_evaluate_rollback(incident_id))`.
15. Tras `REMEDIATION_ROLLBACK_TIMEOUT` segundos (default 300), `_evaluate_rollback` consulta el estado de los pods con `kubectl get pods -n <ns> -l <selector>`.
16. Si **todos** los pods están `Running` con `restartCount==0` → counter `aiops_remediation_rollback_total{outcome=healthy}`, mensaje Mattermost "Remediation healthy".
17. Si algún pod está en estado fallido o ha reiniciado → ejecuta `kubectl set resources deployment <name> -n <ns> --containers=<container> --limits=memory=<pre_patch_value>` (respeta `DRY_RUN`). Counter `outcome=reverted` o `outcome=revert_failed`. Mensaje Mattermost con resultado.

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
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings (768 dims) | 2 |
| `OLLAMA_EMBED_URL` | `http://ollama-svc:11434/api/embeddings` | Endpoint de embeddings | 2 |
| `CHROMADB_HOST` | `chromadb-svc` | Host de ChromaDB | 2 |
| `CHROMADB_PORT` | `8000` | Puerto de ChromaDB | 2 |
| `MATTERMOST_WEBHOOK_URL` | `None` | URL del webhook entrante de Mattermost | 1 |
| `WEBHOOK_SECRET` | `""` | Secreto HMAC-SHA256 para verificar callbacks de botones Mattermost. Vacío = sin verificación (dev/test). En K8s via Secret `agent-secrets.webhook-secret` (`optional: true`) | 3 |
| `REMEDIATION_ROLLBACK_ENABLED` | `true` | Activa el mecanismo de rollback automático post-patch | Mini-Fase 4 |
| `REMEDIATION_ROLLBACK_TIMEOUT` | `300` | Segundos de espera antes de evaluar salud del pod tras un patch | Mini-Fase 4 |
| `REMEDIATION_ROLLBACK_GRACE` | `30` | Segundos de gracia para el rollout antes del health check (reservado, no usado aún en polling) | Mini-Fase 4 |
| `HTTP_TIMEOUT` | `120.0` | Timeout general del cliente HTTP (segundos) | 0+ |
| `HEALTH_TIMEOUT` | `5.0` | Timeout para health checks (segundos) | 0+ |
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

- `aiops_remediation_rollback_total{outcome}` — resultado del rollback post-patch (`scheduled` / `skipped_no_snapshot` / `healthy` / `reverted` / `revert_failed` / `evaluation_error`)

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
