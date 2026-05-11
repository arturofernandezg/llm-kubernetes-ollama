"""
AIOps Infrastructure Agent — Fase 1

Extrae parámetros de infraestructura GCP a partir de mensajes en lenguaje
natural, usando un LLM local (Ollama) como motor de inferencia.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
import httpx
import time
import uuid

from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter

from config import settings, logger
from schemas import (
    InfraRequest, ExtractedParams, ExtractResponse,
    AlertmanagerPayload, AlertItem,
    MattermostActionPayload, ActionCallbackContext,
)
from extraction import PROMPT_TEMPLATE, extract_json
from validation import validate_params
from mattermost import send_mattermost_alert, send_escalation_with_buttons
from rag import (
    build_rag_query, retrieve_context, get_chroma_client, ensure_collections,
    build_incident_document, ingest_incident,
)
from diagnosis import generate_diagnosis
from remediation import process_remediation, execute_commands, RemediationAction

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
DIAGNOSIS_COUNTER = Counter(
    "aiops_diagnosis_total",
    "Diagnosis attempts by outcome",
    ["outcome"],  # "success" | "rag_failed" | "llm_failed" | "pipeline_failed"
)
REMEDIATION_COUNTER = Counter(
    "aiops_remediation_total",
    "Remediation decisions by action",
    ["action"],  # "auto_remediate" | "escalate" | "suggest_only" | "skipped"
)
FEEDBACK_COUNTER = Counter(
    "aiops_feedback_total",
    "Incident persistence attempts",
    ["outcome"],  # "persisted" | "skipped" | "failed"
)


# ── Human Escalation State ────────────────────────────────────────────────────

ESCALATION_TTL_MINUTES = 60


@dataclass
class PendingEscalation:
    incident_id: str
    alert_item: AlertItem
    diagnosis: dict
    safe_commands: list[str]
    expires_at: datetime


PENDING_ESCALATIONS: dict[str, PendingEscalation] = {}
_PENDING_LOCK = asyncio.Lock()


async def _cleanup_expired_escalations() -> None:
    """Elimina escalaciones expiradas del dict en memoria."""
    now = datetime.now()
    async with _PENDING_LOCK:
        expired = [k for k, v in PENDING_ESCALATIONS.items() if v.expires_at < now]
        for k in expired:
            logger.info("Expiring pending escalation", extra={"incident_id": k})
            del PENDING_ESCALATIONS[k]


async def _periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(300)
        await _cleanup_expired_escalations()


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

    # ChromaDB client (fail-open: None if unavailable at startup)
    try:
        app.state.chroma_client = get_chroma_client()
        ensure_collections(app.state.chroma_client)
        logger.info(
            "ChromaDB connected at %s:%s",
            settings.chromadb_host, settings.chromadb_port,
        )
    except Exception as exc:
        app.state.chroma_client = None
        logger.warning("ChromaDB unavailable at startup: %s", exc)

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

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
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


# ── Diagnosis helpers ─────────────────────────────────────────────────────────

def _format_diagnosis_message(
    alert: AlertItem,
    diagnosis: dict | None,
    remediation: dict | None = None,
) -> str:
    """Format alert + diagnosis + remediation into a Mattermost-ready Markdown message."""
    icon = "🔴" if alert.status == "firing" else "🟢"
    severity = alert.labels.get("severity", "critical").upper()
    alert_name = alert.labels.get("alertname", "UnknownAlert")
    pod = alert.labels.get("pod", "unknown-pod")
    namespace = alert.labels.get("namespace", "unknown-ns")

    header = f"{icon} **[{severity}] {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"

    if diagnosis is None:
        desc = alert.annotations.get("description", "Sin descripcion")
        return f"{header}\n> {desc}\n\n_⚠️ Diagnosis unavailable_"

    confidence_pct = int(diagnosis["confidence"] * 100)
    risk = diagnosis["risk"].upper()

    parts = [
        header,
        f"\n**Diagnosis:** {diagnosis['diagnosis']}",
        f"**Risk:** {risk} | **Confidence:** {confidence_pct}%",
    ]

    if diagnosis.get("commands"):
        cmds = "\n".join(f"  {c}" for c in diagnosis["commands"])
        parts.append(f"**Suggested commands:**\n```\n{cmds}\n```")

    if diagnosis.get("explanation"):
        parts.append(f"> {diagnosis['explanation']}")

    if diagnosis.get("rag_sources"):
        sources = ", ".join(diagnosis["rag_sources"])
        parts.append(f"_Sources: {sources}_")

    if remediation is not None:
        action = remediation["action"]
        if action == RemediationAction.AUTO_REMEDIATE:
            parts.append(
                f"\n✅ **Auto-remediated (dry-run):**\n```\n{remediation['execution_log']}\n```"
            )
        elif action == RemediationAction.ESCALATE:
            blocked = remediation.get("blocked_commands", [])
            if blocked:
                blocked_str = "\n".join(f"  ⛔ {c}" for c in blocked)
                parts.append(f"\n⚠️ **Requires human approval:**\n{blocked_str}")
            else:
                parts.append("\n⚠️ **Requires human approval** (high risk)")

    return "\n".join(parts)


def _format_escalation_header(alert: AlertItem) -> str:
    """Línea de título para el mensaje de escalación con botones."""
    alert_name = alert.labels.get("alertname", "UnknownAlert")
    pod = alert.labels.get("pod", "unknown-pod")
    namespace = alert.labels.get("namespace", "unknown-ns")
    severity = alert.labels.get("severity", "critical").upper()
    return f"⚠️ **[{severity}] ESCALATION REQUIRED — {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"


def _format_escalation_body(diagnosis: dict, remediation: dict) -> str:
    """Cuerpo del attachment (sin botones) para el mensaje de escalación."""
    confidence_pct = int(diagnosis["confidence"] * 100)
    risk = diagnosis["risk"].upper()

    parts = [
        f"**Diagnóstico:** {diagnosis['diagnosis']}",
        f"**Risk:** {risk} | **Confidence:** {confidence_pct}%",
    ]

    safe_cmds = remediation.get("safe_commands", [])
    if safe_cmds:
        cmds_str = "\n".join(safe_cmds)
        parts.append(f"**Comandos propuestos (requieren aprobación):**\n```\n{cmds_str}\n```")

    return "\n".join(parts)


async def _process_alert_with_diagnosis(
    alert: AlertItem,
    http_client: httpx.AsyncClient,
    chroma_client,
) -> None:
    """Background task: RAG query → retrieve context → LLM diagnosis → Mattermost."""
    alert_name = alert.labels.get("alertname", "UnknownAlert")
    try:
        description = alert.annotations.get("description", "")
        query = build_rag_query(alert.labels, description)

        # RAG retrieval (fail-open: empty context if ChromaDB down)
        try:
            rag_context = await retrieve_context(query, http_client, chroma_client)
            DIAGNOSIS_COUNTER.labels(outcome="rag_ok").inc()
        except Exception as exc:
            logger.warning("RAG retrieval failed for %s, proceeding without context: %s", alert_name, exc)
            rag_context = {"runbooks": [], "incidents": [], "query": query}
            DIAGNOSIS_COUNTER.labels(outcome="rag_failed").inc()

        # LLM diagnosis (fail-open: None if Ollama down)
        try:
            diagnosis = await generate_diagnosis(
                alert.labels, alert.annotations, alert.status,
                rag_context, http_client,
            )
            DIAGNOSIS_COUNTER.labels(outcome="success").inc()
        except Exception as exc:
            logger.warning("Diagnosis generation failed for %s: %s", alert_name, exc)
            diagnosis = None
            DIAGNOSIS_COUNTER.labels(outcome="llm_failed").inc()

        remediation_result = None
        if diagnosis is not None:
            try:
                remediation_result = await process_remediation(diagnosis)
                REMEDIATION_COUNTER.labels(action=remediation_result["action"].value).inc()
            except Exception as exc:
                logger.warning("Remediation processing failed for %s: %s", alert_name, exc)
                REMEDIATION_COUNTER.labels(action="skipped").inc()

        # Feedback loop: persist incident in ChromaDB (fail-open)
        if diagnosis is not None:
            try:
                alert_data = {"labels": alert.labels, "annotations": alert.annotations}
                doc_id, text, metadata = build_incident_document(
                    alert_data, diagnosis, remediation_result,
                )
                await ingest_incident(
                    doc_id=doc_id, text=text, metadata=metadata,
                    http_client=http_client, chroma_client=chroma_client,
                )
                FEEDBACK_COUNTER.labels(outcome="persisted").inc()
            except Exception as exc:
                logger.warning("Failed to persist incident for %s: %s", alert_name, exc)
                FEEDBACK_COUNTER.labels(outcome="failed").inc()
        else:
            FEEDBACK_COUNTER.labels(outcome="skipped").inc()

        # Escalations with approvable commands → send with interactive buttons
        if (
            remediation_result is not None
            and remediation_result["action"] == RemediationAction.ESCALATE
            and remediation_result.get("safe_commands")
        ):
            incident_id = str(uuid.uuid4())
            async with _PENDING_LOCK:
                PENDING_ESCALATIONS[incident_id] = PendingEscalation(
                    incident_id=incident_id,
                    alert_item=alert,
                    diagnosis=diagnosis,
                    safe_commands=remediation_result["safe_commands"],
                    expires_at=datetime.now() + timedelta(minutes=ESCALATION_TTL_MINUTES),
                )
            logger.info(
                "Stored pending escalation %s for %s (%d commands)",
                incident_id, alert_name, len(remediation_result["safe_commands"]),
            )
            await send_escalation_with_buttons(
                header=_format_escalation_header(alert),
                attachment_text=_format_escalation_body(diagnosis, remediation_result),
                safe_commands=remediation_result["safe_commands"],
                incident_id=incident_id,
                callback_base_url=settings.agent_callback_url,
            )
        else:
            msg = _format_diagnosis_message(alert, diagnosis, remediation_result)
            await send_mattermost_alert(msg)

    except Exception as exc:
        # Last resort: send raw alert
        logger.error("Full diagnosis pipeline failed for %s: %s", alert_name, exc)
        DIAGNOSIS_COUNTER.labels(outcome="pipeline_failed").inc()
        icon = "🔴" if alert.status == "firing" else "🟢"
        severity = alert.labels.get("severity", "critical").upper()
        pod = alert.labels.get("pod", "unknown-pod")
        namespace = alert.labels.get("namespace", "unknown-ns")
        desc = alert.annotations.get("description", "Sin descripcion")
        fallback = f"{icon} **[{severity}] {alert_name}** en Pod `{pod}` (NS: `{namespace}`)\n> {desc}"
        await send_mattermost_alert(fallback)


@app.post(
    "/webhook/alert",
    summary="Recibe alertas de Prometheus Alertmanager (AIOps Ingestion)",
)
async def handle_alert_webhook(payload: AlertmanagerPayload, background_tasks: BackgroundTasks):
    """
    Ingesta el alert-burst de Alertmanager garantizando el Data Contract.
    Registra el evento usando structured logging y delega la emisión
    a Mattermost a una BackgroundTask para evitar latencia al cliente (O(1)).
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

        logger.info(
            "Processing alert %d/%d",
            idx + 1, len(payload.alerts),
            extra={
                "alertname": alert_name,
                "firing_status": alert.status,
            },
        )

        if alert.status == "firing":
            # Full RAG + diagnosis pipeline for firing alerts
            background_tasks.add_task(
                _process_alert_with_diagnosis,
                alert, app.state.http_client, app.state.chroma_client,
            )
        else:
            # Resolved alerts: simple notification, no diagnosis needed
            severity = alert.labels.get("severity", "critical").upper()
            pod = alert.labels.get("pod", "unknown-pod")
            namespace = alert.labels.get("namespace", "unknown-ns")
            msg = f"🟢 **[RESOLVED] [{severity}] {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"
            background_tasks.add_task(send_mattermost_alert, msg)
        
    return {
        "status": "success",
        "alerts_processed": len(payload.alerts),
        "message": "Payload ingested and queued for RAG processing"
    }


@app.post(
    "/webhook/action",
    summary="Recibe callbacks de botones interactivos de Mattermost (escalaciones)",
)
async def handle_action_callback(payload: MattermostActionPayload) -> dict:
    """
    Procesa la decisión del operador sobre una escalación pendiente.
    Mattermost llama a este endpoint cuando el usuario hace clic en Aprobar o Rechazar.
    La respuesta JSON actualiza el mensaje original y limpia los botones.
    """
    async with _PENDING_LOCK:
        incident = PENDING_ESCALATIONS.get(payload.context.incident_id)
        if incident is None:
            logger.warning(
                "Unknown or expired incident_id in callback",
                extra={"incident_id": payload.context.incident_id},
            )
            return {
                "ephemeral_text": "Escalación no encontrada o expirada. No se tomó ninguna acción.",
                "update": {
                    "message": "⏰ Escalación expirada o no encontrada — no se tomó ninguna acción.",
                    "props": {"attachments": []},
                },
            }
        if datetime.now() > incident.expires_at:
            del PENDING_ESCALATIONS[payload.context.incident_id]
            logger.warning(
                "Escalation expired (TTL %dm)",
                ESCALATION_TTL_MINUTES,
                extra={"incident_id": incident.incident_id},
            )
            return {
                "ephemeral_text": "Escalación expirada. Por favor, espera una nueva alerta.",
                "update": {
                    "message": "⏰ Escalación expirada (TTL superado) — no se tomó ninguna acción.",
                    "props": {"attachments": []},
                },
            }
        PENDING_ESCALATIONS.pop(payload.context.incident_id)

    user = payload.user_name or "human"
    action = payload.context.action

    if action == "approve":
        try:
            log = await execute_commands(incident.safe_commands)
        except Exception as exc:
            logger.error("Command execution failed for incident %s: %s", incident.incident_id, exc)
            log = f"ERROR: {exc}"
        REMEDIATION_COUNTER.labels(action="human_approved").inc()
        decision_line = f"\n---\n✅ **Remediación APROBADA** por @{user}\n```\n{log}\n```"
        remediation_for_feedback = {
            "action": RemediationAction.AUTO_REMEDIATE,
            "execution_log": log,
            "safe_commands": incident.safe_commands,
            "blocked_commands": [],
        }
        logger.info("Human approved remediation for incident %s", incident.incident_id)
    else:
        REMEDIATION_COUNTER.labels(action="human_rejected").inc()
        decision_line = f"\n---\n❌ **Remediación RECHAZADA** por @{user}"
        remediation_for_feedback = {
            "action": RemediationAction.ESCALATE,
            "execution_log": "",
            "safe_commands": incident.safe_commands,
            "blocked_commands": [],
        }
        logger.info(
            "Human rejected remediation for incident %s (action=%s)",
            incident.incident_id, action,
        )

    # Rebuild full message: original alert context + decision appended at bottom
    original_header = _format_escalation_header(incident.alert_item)
    original_body = _format_escalation_body(incident.diagnosis, remediation_for_feedback)
    update_msg = f"{original_header}\n\n{original_body}{decision_line}"

    # Persist final human decision to ChromaDB (fail-open)
    try:
        alert_data = {
            "labels": incident.alert_item.labels,
            "annotations": incident.alert_item.annotations,
        }
        doc_id, text, metadata = build_incident_document(
            alert_data, incident.diagnosis, remediation_for_feedback,
        )
        await ingest_incident(
            doc_id=doc_id, text=text, metadata=metadata,
            http_client=app.state.http_client,
            chroma_client=app.state.chroma_client,
        )
        FEEDBACK_COUNTER.labels(outcome="persisted").inc()
    except Exception as exc:
        logger.warning("Failed to persist human decision for %s: %s", incident.incident_id, exc)
        FEEDBACK_COUNTER.labels(outcome="failed").inc()

    # Response updates original Mattermost message and clears action buttons
    return {
        "update": {
            "message": update_msg,
            "props": {"attachments": []},
        }
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
