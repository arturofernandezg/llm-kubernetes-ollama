"""
AIOps Infrastructure Agent — Fase 1

Extrae parámetros de infraestructura GCP a partir de mensajes en lenguaje
natural, usando un LLM local (Ollama) como motor de inferencia.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
import httpx
import time
import uuid

from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter

from config import settings, logger
from schemas import (
    InfraRequest, ExtractedParams, ExtractResponse,
    AlertmanagerPayload
)
from extraction import PROMPT_TEMPLATE, extract_json
from validation import validate_params

# ── Métricas Prometheus ──────────────────────────────────────────────────────
RETRY_COUNTER = Counter(
    "aiops_ollama_retries_total",
    "Number of Ollama retry attempts",
    ["outcome"],  # "success" | "exhausted"
)
EXTRACTION_COUNTER = Counter(
    "aiops_extraction_total",
    "Extraction attempts by method",
    ["method"],  # "direct" | "markdown_block" | "regex_search" | "failed"
)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la app:
    - Crea un cliente HTTP compartido (reutiliza conexiones, no abre uno por request).
    - Verifica conectividad con Ollama al arrancar.
    - Cierra el cliente limpiamente al apagar.
    """
    logger.info(
        "Agent starting — model: %s, ollama: %s",
        settings.ollama_model, settings.ollama_url,
    )

    app.state.http_client = httpx.AsyncClient(timeout=settings.http_timeout)

    try:
        r = await app.state.http_client.get(settings.ollama_tags, timeout=10.0)
        r.raise_for_status()
        available = [m["name"] for m in r.json().get("models", [])]
        if any(settings.ollama_model in m for m in available):
            logger.info("Model '%s' confirmed available", settings.ollama_model)
        else:
            logger.warning(
                "Model '%s' NOT found. Available: %s",
                settings.ollama_model, available,
            )
    except Exception as exc:
        logger.warning("Could not reach Ollama at startup: %s", exc)

    yield

    await app.state.http_client.aclose()
    logger.info("Agent shutting down — HTTP client closed")


app = FastAPI(
    title="AIOps Infrastructure Agent",
    description="Extrae parámetros de infraestructura GCP desde lenguaje natural.",
    version="0.4.0",
    lifespan=lifespan,
)

# Auto-instrumentar todos los endpoints: request count, latency histogram, in-progress
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# ── Middleware ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with method, path, status and duration (JSON structured)."""
    start = time.time()
    response = await call_next(request)
    duration = int((time.time() - start) * 1000)
    logger.info(
        "request completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration,
        },
    )
    return response


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/healthz", summary="Liveness probe — solo verifica que el proceso responde")
async def healthz():
    """
    Liveness probe: responde 200 siempre que el proceso esté vivo.
    NO comprueba dependencias externas (Ollama, etc.).
    Kubernetes usa este endpoint para decidir si reiniciar el pod.
    """
    return {"status": "alive"}


@app.get("/readyz", summary="Readiness probe — verifica Ollama + modelo disponible")
async def readyz():
    """
    Readiness probe: verifica que Ollama es alcanzable y el modelo está cargado.
    Devuelve 503 si Ollama no responde o el modelo no está disponible.
    Kubernetes usa este endpoint para decidir si enrutar tráfico al pod.
    """
    try:
        client: httpx.AsyncClient = app.state.http_client
        r = await client.get(settings.ollama_tags, timeout=settings.health_timeout)
        r.raise_for_status()
        available = [m["name"] for m in r.json().get("models", [])]
        model_loaded = any(settings.ollama_model in m for m in available)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {exc}")

    if not model_loaded:
        raise HTTPException(
            status_code=503,
            detail=f"Model '{settings.ollama_model}' not loaded. Available: {available}",
        )

    return {
        "status": "ready",
        "model": settings.ollama_model,
        "model_loaded": model_loaded,
    }


@app.get("/health", include_in_schema=False)
async def health():
    """Deprecated — redirige a /readyz para retrocompatibilidad."""
    return RedirectResponse(url="/readyz", status_code=307)


@app.post(
    "/webhook/alert",
    summary="Recibe alertas de Prometheus Alertmanager (AIOps Ingestion)",
)
async def handle_alert_webhook(payload: AlertmanagerPayload):
    """
    Ingesta el alert-burst de Alertmanager garantizando el Data Contract.
    Registra el evento usando structured logging y prepara la alerta
    para la Fase de RAG/Mattermost.
    """
    logger.info(
        "Alert webhook received",
        extra={
            "alert_status": payload.status,
            "alerts_count": len(payload.alerts),
            "receiver": payload.receiver,
        },
    )
    
    for idx, alert in enumerate(payload.alerts):
        alert_name = alert.labels.get("alertname", "UnknownAlert")
        pod = alert.labels.get("pod", "unknown-pod")
        namespace = alert.labels.get("namespace", "unknown-ns")
        
        logger.info(
            f"Processing alert {idx+1}/{len(payload.alerts)}",
            extra={
                "alertname": alert_name,
                "target_pod": pod,
                "target_namespace": namespace,
                "firing_status": alert.status,
            }
        )
        
        # TODO: Fase 2 - Buscar contexto de remediación en ChromaDB
        # TODO: Fase 2 - Evaluar con LLM
        # TODO: Fase 3 - Notificar a Mattermost o parchear K8s
        
    return {
        "status": "success",
        "alerts_processed": len(payload.alerts),
        "message": "Payload ingested and queued for RAG processing"
    }


@app.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Extrae parámetros de infraestructura desde lenguaje natural",
)
async def extract_parameters(request: InfraRequest):
    """
    Recibe un mensaje en lenguaje natural, lo envía al LLM y devuelve
    los parámetros de infraestructura extraídos en formato estructurado.
    """
    request_id = str(uuid.uuid4())[:8]
    start = time.time()
    logger.info("[%s] Processing: %s", request_id, request.message[:100])

    prompt = PROMPT_TEMPLATE.format(user_request=request.message)
    client: httpx.AsyncClient = app.state.http_client

    # Retry con exponential backoff para errores transitorios
    last_exc: Exception | None = None
    response = None

    for attempt in range(settings.retry_max_attempts):
        try:
            response = await client.post(
                settings.ollama_url,
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            break  # éxito, salir del loop
        except httpx.HTTPStatusError as exc:
            # Error del modelo (4xx/5xx) — no reintentar
            logger.error("[%s] Ollama HTTP error: %s", request_id, exc.response.status_code)
            raise HTTPException(
                status_code=502, detail=f"LLM returned error: {exc.response.status_code}"
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < settings.retry_max_attempts - 1:
                delay = min(
                    settings.retry_base_delay * (2 ** attempt),
                    settings.retry_max_delay,
                )
                logger.warning(
                    "[%s] Ollama attempt %d/%d failed (%s), retrying in %.1fs",
                    request_id, attempt + 1, settings.retry_max_attempts,
                    type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[%s] Ollama failed after %d attempts: %s",
                    request_id, settings.retry_max_attempts, exc,
                )
        except httpx.HTTPError as exc:
            logger.error("[%s] Ollama connection error: %s", request_id, exc)
            raise HTTPException(status_code=502, detail=f"LLM unavailable: {exc}")

    if response is None:
        RETRY_COUNTER.labels(outcome="exhausted").inc()
        if isinstance(last_exc, httpx.TimeoutException):
            raise HTTPException(status_code=504, detail="LLM timeout — model took too long")
        raise HTTPException(status_code=502, detail=f"LLM unavailable after {settings.retry_max_attempts} attempts: {last_exc}")

    RETRY_COUNTER.labels(outcome="success").inc()

    raw = response.json().get("response", "")
    parsed_dict, method = extract_json(raw)
    EXTRACTION_COUNTER.labels(method=method or "failed").inc()
    warnings = (
        validate_params(parsed_dict)
        if parsed_dict
        else ["Could not extract JSON from model response"]
    )

    duration_ms = int((time.time() - start) * 1000)

    if parsed_dict:
        logger.info("[%s] OK via '%s' in %dms — %s", request_id, method, duration_ms, parsed_dict)
    else:
        logger.warning("[%s] Failed in %dms. Raw: %s", request_id, duration_ms, raw[:150])

    return ExtractResponse(
        request_id=request_id,
        input_message=request.message,
        extracted_parameters=ExtractedParams(**parsed_dict) if parsed_dict else None,
        validation_warnings=warnings,
        raw_response=raw,
        model_used=settings.ollama_model,
        extraction_method=method,
        duration_ms=duration_ms,
    )
