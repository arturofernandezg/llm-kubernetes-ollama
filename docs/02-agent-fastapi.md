# Agente FastAPI — Extracción de parámetros

## Archivo principal

`agent/main.py` — versión 0.2.0

## Endpoints

| Método | Path | Descripción | Dependencias |
|---|---|---|---|
| GET | `/healthz` | Liveness probe. Siempre 200. | Ninguna |
| GET | `/readyz` | Readiness probe. 200 si Ollama + modelo OK. | Ollama |
| GET | `/health` | Health completo (retrocompatibilidad). | Ollama |
| POST | `/extract` | Extracción de parámetros desde texto. | Ollama |

## Flujo de /extract

1. Recibe `{"message": "texto en lenguaje natural"}` (validado por Pydantic, max 2000 chars)
2. Construye prompt con `PROMPT_TEMPLATE` + delimitadores `<user_message>...</user_message>`
3. Envía al LLM via cliente httpx compartido (`app.state.http_client`)
4. Intenta extraer JSON de la respuesta con 3 estrategias:
   - **direct**: parseo directo (el LLM devolvió JSON puro)
   - **markdown_block**: JSON dentro de ```json ... ```
   - **regex_search**: primer `{...}` encontrado en el texto
5. Valida parámetros extraídos contra valores conocidos GCP
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

- **Regiones permitidas**: europe-west1/2/3/4, europe-southwest1, us-central1,
  us-east1, us-west1, asia-east1, asia-northeast1
- **Prefijos de instancia válidos**: e2-, n1-, n2-, n2d-, c2-, m1-, t2d-
- **Campos obligatorios**: project_name, region, instance_type, purpose
- No bloquea: genera warnings informativos

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint de generación |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint de modelos |
| `OLLAMA_MODEL` | `tinyllama` | Modelo a usar |

## Cliente HTTP compartido

Creado en el lifespan de FastAPI, cerrado al apagar:
- Timeout de 120s para inferencia (POST /extract)
- Timeout de 5-10s para health checks (GET /readyz, /health)
- Reutiliza pool de conexiones TCP (no crea uno por request)

## Manejo de errores

| Error | HTTP | Detalle |
|---|---|---|
| Ollama timeout (120s) | 504 | "LLM timeout — model took too long" |
| Ollama HTTP error | 502 | "LLM returned error: {status_code}" |
| Ollama connection error | 502 | "LLM unavailable: {error}" |
| Input vacío/largo | 422 | Validación Pydantic |
