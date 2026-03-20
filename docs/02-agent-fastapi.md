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
| `rag.py` | **(Planificado)** Cliente ChromaDB, ingesta, query, construcción de queries | 2 |
| `diagnosis.py` | **(Planificado)** Prompt AIOps contextual, parsing JSON estructurado del LLM | 2 |
| `remediation.py` | **(Planificado)** Validation layer, whitelist comandos, cliente K8s API | 3 |

## Endpoints

| Método | Path | Descripción | Dependencias | Fase |
|---|---|---|---|---|
| GET | `/healthz` | Liveness probe. Siempre 200. | Ninguna | 0 |
| GET | `/readyz` | Readiness probe. 200 si Ollama + modelo OK. | Ollama | 0 |
| POST | `/webhook/alert` | Ingesta alertas Alertmanager → normaliza → RAG → diagnóstico → Mattermost | Ollama, ChromaDB, Mattermost | 1-3 |
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

**Fase 3 (planificado)**:
9. Validation layer evalúa commands contra whitelist/blacklist.
10. Si `risk == "low"` Y `confidence >= 0.8` Y cambio `< 25%` → auto-patch via K8s API.
11. Si no → mensaje a Mattermost con botones `[Aprobar]` / `[Rechazar]`.
12. Resultado (success/failure/rejected) se persiste en colección `incidents` de ChromaDB.

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

### Diagnóstico AIOps (Fase 2 — planificado)

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
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | **(Planificado)** Modelo de embeddings | 2 |
| `OLLAMA_EMBED_URL` | `http://ollama-svc:11434/api/embeddings` | **(Planificado)** Endpoint de embeddings | 2 |
| `CHROMADB_URL` | `http://chromadb-svc:8000` | **(Planificado)** URL de ChromaDB | 2 |
| `MATTERMOST_WEBHOOK_URL` | `None` | URL del webhook entrante de Mattermost | 1 |
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
