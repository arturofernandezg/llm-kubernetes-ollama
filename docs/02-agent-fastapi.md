# Agente FastAPI — Extracción de parámetros

## Estructura modular (`agent/`)

Versión actual: 0.4.0

El agente se modularizó (commit 7ec4a3a) para separar responsabilidades. Originalmente
todo estaba en `main.py`; ahora se divide en 6 módulos:

| Módulo | Responsabilidad |
|---|---|
| `main.py` | FastAPI app, endpoints, lifespan, middleware de logging, retry logic |
| `config.py` | Pydantic BaseSettings, setup de logging JSON estructurado |
| `schemas.py` | Modelos Pydantic v2 (InfraRequest, ExtractedParams, ExtractResponse) |
| `extraction.py` | PROMPT_TEMPLATE, `extract_json()` con 3 estrategias de fallback |
| `validation.py` | `validate_params()` — validación no-bloqueante contra valores GCP |
| `tf_generator.py` | `safe_name()`, `generate_terraform()`, template Terraform |

## Endpoints

| Método | Path | Descripción | Dependencias |
|---|---|---|---|
| GET | `/healthz` | Liveness probe. Siempre 200. | Ninguna |
| GET | `/readyz` | Readiness probe. 200 si Ollama + modelo OK. | Ollama |
| GET | `/health` | Redirect 307 a /readyz (retrocompatibilidad). | Ollama |
| POST | `/extract` | Extracción de parámetros desde texto. | Ollama |
| GET | `/metrics` | Métricas Prometheus (auto-instrumentado + contadores custom). | Ninguna |

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

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint de generación |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint de modelos |
| `OLLAMA_MODEL` | `tinyllama` | Modelo a usar (en K8s se sobreescribe a `qwen2.5:1.5b`) |
| `HTTP_TIMEOUT` | `120.0` | Timeout general del cliente HTTP (segundos) |
| `HEALTH_TIMEOUT` | `5.0` | Timeout para health checks (segundos) |
| `RETRY_MAX_ATTEMPTS` | `3` | Intentos máximos de retry hacia Ollama |
| `RETRY_BASE_DELAY` | `1.0` | Delay base (segundos) para backoff exponencial |
| `RETRY_MAX_DELAY` | `10.0` | Delay máximo (segundos) entre reintentos |
| `LOG_LEVEL` | `INFO` | Nivel de logging (DEBUG, INFO, WARNING, ERROR) |

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
