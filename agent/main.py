"""
AIOps Infrastructure Agent — Fase 1

Extrae parámetros de infraestructura GCP a partir de mensajes en lenguaje
natural, usando un LLM local (Ollama) como motor de inferencia.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
import httpx
import time
import uuid

from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

import redis.asyncio as aioredis

from config import settings, logger
from schemas import (
    InfraRequest, ExtractedParams, ExtractResponse,
    AlertmanagerPayload, AlertItem,
    MattermostActionPayload,
)
from extraction import PROMPT_TEMPLATE, extract_json
from validation import validate_params
from mattermost import send_mattermost_alert, send_escalation_with_buttons, make_hmac_token
from rag import (
    build_rag_query, retrieve_context, get_chroma_client, ensure_collections,
    build_incident_document, ingest_incident, COLLECTION_INCIDENTS,
)
from diagnosis import generate_diagnosis
from remediation import (
    process_remediation, execute_commands, results_to_log, RemediationAction,
    capture_pre_patch_value, check_pod_health, revert_patch, PrePatchSnapshot,
)
from utils import backoff_delay
from escalation_store import store_escalation, get_escalation, delete_escalation, count_escalations
from streams import (
    enqueue_alert, ensure_group, consume_loop, reclaim_pending, QUEUE_PROCESSED,
)

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
    ["outcome"],  # "success" | "rag_ok" | "rag_reconnect" | "rag_failed" | "llm_timeout" | "llm_error" | "pipeline_failed"
)
REMEDIATION_COUNTER = Counter(
    "aiops_remediation_total",
    "Remediation decisions by action",
    ["action"],  # "auto_remediate" | "escalate" | "suggest_only" | "skipped" | "human_approved" | "human_rejected" | "human_approve_failed"
)
FEEDBACK_COUNTER = Counter(
    "aiops_feedback_total",
    "Incident persistence attempts",
    ["outcome"],  # "persisted" | "skipped" | "failed"
)

# ── Métricas Chaos Engineering ───────────────────────────────────────────────
# Activas solo cuando la alerta proviene del namespace arturo-chaos.
CHAOS_EXPERIMENT_COUNTER = Counter(
    "aiops_chaos_experiment_total",
    "Chaos experiments processed by the agent",
    ["experiment", "outcome"],
)
CHAOS_MTTD_HISTOGRAM = Histogram(
    "aiops_chaos_mttd_seconds",
    "Mean Time To Detect: seconds from alert.startsAt to agent webhook receipt",
    ["experiment"],
    buckets=[10, 30, 60, 120, 300],
)
CHAOS_MTTR_HISTOGRAM = Histogram(
    "aiops_chaos_mttr_seconds",
    "Mean Time To Respond: seconds from alert.startsAt to pipeline completion",
    ["experiment"],
    buckets=[30, 60, 180, 300, 600],
)

# ── Rollback metrics ─────────────────────────────────────────────────────────
ROLLBACK_COUNTER = Counter(
    "aiops_remediation_rollback_total",
    "Rollback evaluations by outcome",
    ["outcome"],  # scheduled | skipped_disabled | skipped_no_snapshot | skipped_no_execution | healthy | reverted | revert_failed | evaluation_error
)
WEBHOOK_COUNTER = Counter(
    "aiops_webhook_requests_total",
    "Alert webhook requests received by the agent",
    ["status"],  # "success" | "error"
)
DEDUP_COUNTER = Counter(
    "aiops_dedup_skipped_total",
    "Alert processing skipped due to in-flight duplicate",
    ["alertname"],
)
ESCALATION_STORE_COUNTER = Counter(
    "aiops_escalation_store_total",
    "Escalation persistence attempts by outcome",
    ["outcome"],  # "stored" | "redis_down"
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


# In-flight dedup registry — prevents duplicate LLM calls for the same alert.
# Key: (alertname, namespace, pod). Cleared in the finally of _process_alert_with_diagnosis.
IN_FLIGHT_ALERTS: set[tuple[str, str, str]] = set()
_INFLIGHT_LOCK = asyncio.Lock()


# ── Rollback State ────────────────────────────────────────────────────────────

@dataclass
class RollbackContext:
    incident_id: str
    snapshot: PrePatchSnapshot
    alert_item: AlertItem
    diagnosis: dict
    scheduled_at: datetime


IN_FLIGHT_ROLLBACKS: dict[str, RollbackContext] = {}
_ROLLBACK_LOCK = asyncio.Lock()


def _verify_hmac_token(incident_id: str, action: str, token: str | None) -> bool:
    """Returns True if HMAC is valid or HMAC is disabled (webhook_secret empty)."""
    if not settings.webhook_secret:
        return True
    if not token:
        return False
    expected = make_hmac_token(incident_id, action, settings.webhook_secret)
    return hmac.compare_digest(token, expected)


def _escalation_to_dict(esc: PendingEscalation) -> dict:
    """Serialize PendingEscalation to a JSON-serializable dict for Redis storage."""
    return {
        "incident_id": esc.incident_id,
        "alert_item": esc.alert_item.model_dump(mode="json"),
        "diagnosis": esc.diagnosis,
        "safe_commands": esc.safe_commands,
        "expires_at": esc.expires_at.isoformat(),
    }


def _dict_to_escalation(data: dict) -> PendingEscalation:
    """Deserialize a Redis-stored dict back to PendingEscalation."""
    return PendingEscalation(
        incident_id=data["incident_id"],
        alert_item=AlertItem(**data["alert_item"]),
        diagnosis=data["diagnosis"],
        safe_commands=data["safe_commands"],
        expires_at=datetime.fromisoformat(data["expires_at"]),
    )


async def _cleanup_expired_rollbacks() -> None:
    """Elimina rollbacks cuya ventana de evaluación ha expirado sin ser procesados."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.remediation_rollback_timeout * 2)
    async with _ROLLBACK_LOCK:
        expired = [k for k, v in IN_FLIGHT_ROLLBACKS.items() if v.scheduled_at < cutoff]
        for k in expired:
            logger.warning("Expiring stale in-flight rollback", extra={"incident_id": k})
            del IN_FLIGHT_ROLLBACKS[k]


async def _schedule_rollback_evaluation(
    incident_id: str,
    snapshot: PrePatchSnapshot,
    alert_item: AlertItem,
    diagnosis: dict,
) -> None:
    """Register a rollback context and schedule the evaluation task."""
    async with _ROLLBACK_LOCK:
        IN_FLIGHT_ROLLBACKS[incident_id] = RollbackContext(
            incident_id=incident_id,
            snapshot=snapshot,
            alert_item=alert_item,
            diagnosis=diagnosis,
            scheduled_at=datetime.now(timezone.utc),
        )
    ROLLBACK_COUNTER.labels(outcome="scheduled").inc()
    logger.info(
        "Rollback evaluation scheduled",
        extra={"incident_id": incident_id, "timeout_s": settings.remediation_rollback_timeout},
    )
    asyncio.create_task(_evaluate_rollback(incident_id))


async def _evaluate_rollback(incident_id: str) -> None:
    """Wait REMEDIATION_ROLLBACK_TIMEOUT seconds, then check pod health and revert if needed."""
    try:
        await asyncio.sleep(settings.remediation_rollback_timeout)

        async with _ROLLBACK_LOCK:
            ctx = IN_FLIGHT_ROLLBACKS.get(incident_id)

        if ctx is None:
            logger.info("Rollback context already cleared", extra={"incident_id": incident_id})
            return

        health = await check_pod_health(ctx.snapshot)
        logger.info(
            "Rollback evaluation: pod health check",
            extra={
                "incident_id": incident_id,
                "healthy": health.healthy,
                "reason": health.reason,
                "phases": health.observed_phases,
            },
        )

        _, alert_name, _, _ = _extract_alert_meta(ctx.alert_item)

        if health.healthy:
            ROLLBACK_COUNTER.labels(outcome="healthy").inc()
            msg = (
                f"✅ **Remediation healthy** — `{alert_name}`\n"
                f"Pod(s) running normally {settings.remediation_rollback_timeout}s after patch. "
                f"No rollback needed.\n"
                f"_Deployment: `{ctx.snapshot.deployment}` · NS: `{ctx.snapshot.namespace}`_"
            )
            await send_mattermost_alert(msg)
        else:
            result = await revert_patch(ctx.snapshot)
            if result.success:
                ROLLBACK_COUNTER.labels(outcome="reverted").inc()
                msg = (
                    f"🔄 **Rollback executed** — `{alert_name}`\n"
                    f"Pod(s) still failing after {settings.remediation_rollback_timeout}s "
                    f"(reason: `{health.reason}`). "
                    f"Reverted `{ctx.snapshot.deployment}` memory to `{ctx.snapshot.value}`.\n"
                    f"```\n{results_to_log([result])}\n```"
                )
            else:
                ROLLBACK_COUNTER.labels(outcome="revert_failed").inc()
                msg = (
                    f"⚠️ **Rollback FAILED** — `{alert_name}`\n"
                    f"Pod(s) unhealthy (reason: `{health.reason}`) AND revert command failed.\n"
                    f"Manual intervention required on `{ctx.snapshot.deployment}` in NS `{ctx.snapshot.namespace}`.\n"
                    f"```\n{results_to_log([result])}\n```"
                )
            await send_mattermost_alert(msg)
            logger.warning(
                "Rollback outcome",
                extra={
                    "incident_id": incident_id,
                    "outcome": "reverted" if result.success else "revert_failed",
                    "deployment": ctx.snapshot.deployment,
                    "namespace": ctx.snapshot.namespace,
                    "pre_patch_value": ctx.snapshot.value,
                },
            )

    except Exception as exc:
        ROLLBACK_COUNTER.labels(outcome="evaluation_error").inc()
        logger.error(
            "Rollback evaluation error",
            extra={"incident_id": incident_id, "error": str(exc)},
        )
    finally:
        async with _ROLLBACK_LOCK:
            IN_FLIGHT_ROLLBACKS.pop(incident_id, None)


async def _periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(300)
        await _cleanup_expired_rollbacks()


async def _periodic_reclaim() -> None:
    """
    Recupera entradas pendientes del stream (F2 Slice 2): primera iteración
    inmediata (recuperación de arranque tras un reinicio del pod) y luego cada
    `queue_reclaim_interval_seconds`. Fail-soft por iteración: un fallo de Redis
    no mata la tarea, se reintenta al próximo tick.
    """
    while True:
        try:
            await reclaim_pending(app.state.redis, _handle_stream_entry)
        except Exception as exc:
            logger.error("Periodic reclaim failed: %r", exc)
        await asyncio.sleep(settings.queue_reclaim_interval_seconds)


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

    # Redis client for persistent escalation storage (fail-open: None if unavailable)
    try:
        redis_client = aioredis.Redis(
            host=settings.redis_host, port=settings.redis_port,
            decode_responses=True,
        )
        await redis_client.ping()
        app.state.redis = redis_client
        logger.info("Redis connected at %s:%s", settings.redis_host, settings.redis_port)
    except Exception as exc:
        app.state.redis = None
        logger.warning("Redis unavailable at startup — escalation persistence disabled: %s", exc)

    # Redis Streams consumer (F2) — only when the queue is enabled and Redis is up.
    app.state.consumer_task = None
    app.state.reclaim_task = None
    if settings.queue_enabled and app.state.redis is not None:
        try:
            await ensure_group(app.state.redis)
            app.state.consumer_task = asyncio.create_task(
                consume_loop(app.state.redis, _handle_stream_entry)
            )
            app.state.reclaim_task = asyncio.create_task(_periodic_reclaim())
            logger.info("Alert queue enabled — stream consumer + reclaim started")
        except Exception as exc:
            logger.error("Failed to start stream consumer: %r", exc)
    elif settings.queue_enabled:
        logger.warning("QUEUE_ENABLED but Redis unavailable — falling back to background tasks")

    try:
        r = await app.state.http_client.get(settings.ollama_tags, timeout=10.0)
        r.raise_for_status()
        available = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
        if any(settings.ollama_model in m for m in available):
            logger.info("Model '%s' confirmed available", settings.ollama_model)
        else:
            logger.warning(
                "Model '%s' NOT found. Available: %s",
                settings.ollama_model, available,
            )
    except Exception as exc:
        logger.warning("Could not reach Ollama at startup: %s", exc)

    if not settings.webhook_secret:
        logger.warning(
            "WEBHOOK_SECRET not configured — Mattermost button callbacks accept any token"
        )

    if not settings.mm_command_token:
        logger.warning(
            "MM_COMMAND_TOKEN not configured — /aiops slash command accepts any token"
        )

    # Pre-initialize labeled counters so they appear in /metrics from startup.
    # prometheus_client only emits label combinations after the first observation.
    for _action in ("auto_remediate", "escalate", "suggest_only"):
        REMEDIATION_COUNTER.labels(action=_action).inc(0)
    for _outcome in ("scheduled", "healthy", "reverted", "revert_failed"):
        ROLLBACK_COUNTER.labels(outcome=_outcome).inc(0)
    WEBHOOK_COUNTER.labels(status="success").inc(0)
    WEBHOOK_COUNTER.labels(status="error").inc(0)
    QUEUE_PROCESSED.labels(outcome="success").inc(0)
    QUEUE_PROCESSED.labels(outcome="error").inc(0)

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    for _task in (app.state.consumer_task, app.state.reclaim_task):
        if _task is not None:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
    await app.state.http_client.aclose()
    if app.state.redis is not None:
        await app.state.redis.aclose()
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


@app.get("/readyz", summary="Readiness probe — Redis (cola) o Ollama+modelo (legacy)")
async def readyz():
    """
    Readiness probe condicional según el modo de ingesta:

    - Cola activa (``queue_enabled``): la dependencia crítica de ingesta es Redis
      (el webhook encola ahí). Se chequea solo Redis. Ollama lento/caído NO debe
      sacar al pod de rotación — la cola está para bufferear esa lentitud; procesar
      es problema del worker, no del ingress.
    - Legacy (``queue_enabled=False``, default): se chequea Ollama alcanzable +
      modelo cargado, como hasta ahora.

    Devuelve 503 si la dependencia del modo activo no está sana.
    Kubernetes usa este endpoint para decidir si enrutar tráfico al pod.
    """
    if settings.queue_enabled:
        redis_client = getattr(app.state, "redis", None)
        if redis_client is None:
            raise HTTPException(status_code=503, detail="Redis unavailable (queue mode)")
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=settings.health_timeout)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Redis unreachable: {exc}")
        return {"status": "ready", "mode": "queue", "redis": "up"}

    try:
        client: httpx.AsyncClient = app.state.http_client
        r = await client.get(settings.ollama_tags, timeout=settings.health_timeout)
        r.raise_for_status()
        available = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
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

def _extract_alert_meta(alert: AlertItem) -> tuple[str, str, str, str]:
    """Returns (severity_upper, alert_name, pod, namespace) from alert labels."""
    return (
        alert.labels.get("severity", "critical").upper(),
        alert.labels.get("alertname", "UnknownAlert"),
        alert.labels.get("pod", "unknown-pod"),
        alert.labels.get("namespace", "unknown-ns"),
    )


def _format_diagnosis_message(
    alert: AlertItem,
    diagnosis: dict | None,
    remediation: dict | None = None,
    llm_timeout: bool = False,
) -> str:
    """Format alert + diagnosis + remediation into a Mattermost-ready Markdown message."""
    icon = "🔴" if alert.status == "firing" else "🟢"
    severity, alert_name, pod, namespace = _extract_alert_meta(alert)

    header = f"{icon} **[{severity}] {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"

    if diagnosis is None:
        desc = alert.annotations.get("description", "Sin descripcion")
        if llm_timeout:
            return (
                f"{header}\n"
                f"⏱️ **LLM timeout** — el modelo tardó más de {int(settings.http_timeout)}s y no generó diagnóstico.\n"
                f"Alerta cruda:\n> {desc}"
            )
        return f"{header}\n> {desc}\n\n_⚠️ Diagnosis unavailable_"

    confidence_pct = int(diagnosis.get("confidence", 0.0) * 100)
    risk = diagnosis.get("risk", "high").upper()

    parts = [
        header,
        f"\n**Diagnosis:** {diagnosis.get('diagnosis', 'Sin diagnóstico')}",
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
    severity, alert_name, pod, namespace = _extract_alert_meta(alert)
    return f"⚠️ **[{severity}] ESCALATION REQUIRED — {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"


def _format_escalation_body(diagnosis: dict, remediation: dict) -> str:
    """Cuerpo del attachment (sin botones) para el mensaje de escalación."""
    confidence_pct = int(diagnosis.get("confidence", 0.0) * 100)
    risk = diagnosis.get("risk", "high").upper()

    parts = [
        f"**Diagnóstico:** {diagnosis.get('diagnosis', 'Sin diagnóstico')}",
        f"**Risk:** {risk} | **Confidence:** {confidence_pct}%",
    ]

    safe_cmds = remediation.get("safe_commands", [])
    if safe_cmds:
        cmds_str = "\n".join(safe_cmds)
        parts.append(f"**Comandos propuestos (requieren aprobación):**\n```\n{cmds_str}\n```")

    return "\n".join(parts)


# ── Slash command helpers ─────────────────────────────────────────────────────

def _query_recent_incidents(chroma_client, limit: int) -> list[dict]:
    """Return last `limit` incidents from ChromaDB sorted by timestamp desc.

    Fail-open: returns [] if ChromaDB unavailable or collection empty.
    """
    if chroma_client is None:
        return []
    try:
        coll = chroma_client.get_or_create_collection(
            name=COLLECTION_INCIDENTS, metadata={"hnsw:space": "cosine"},
        )
        if coll.count() == 0:
            return []
        raw = coll.get(include=["documents", "metadatas"])
        rows = [
            {"id": rid, "document": doc, "metadata": meta}
            for rid, doc, meta in zip(raw["ids"], raw["documents"], raw["metadatas"])
        ]
        rows.sort(key=lambda r: int(r["metadata"].get("timestamp", 0)), reverse=True)
        return rows[:limit]
    except Exception as exc:
        logger.warning("Failed to query recent incidents: %s", exc)
        return []


def _format_status_response(ollama_ok: bool, chroma_ok: bool, pending_count: int = 0) -> str:
    ollama = "UP" if ollama_ok else "DOWN"
    chroma = "UP" if chroma_ok else "DOWN"
    mode = "DRY-RUN" if settings.remediation_dry_run else "LIVE"
    return (
        f"**AIOps Agent — Status**\n"
        f"- Ollama: {ollama} ({settings.ollama_model})\n"
        f"- ChromaDB: {chroma}\n"
        f"- Remediation: {'enabled' if settings.remediation_enabled else 'disabled'} ({mode})\n"
        f"- Pending escalations: {pending_count}\n"
    )


def _format_incidents_response(incidents: list[dict]) -> str:
    if not incidents:
        return "_No incidents persisted yet._"
    lines = ["**Last incidents:**"]
    for row in incidents:
        meta = row["metadata"]
        ts = meta.get("timestamp", "?")
        outcome = meta.get("outcome", "?")
        err = meta.get("error_class", "?")
        conf = meta.get("confidence", 0.0)
        lines.append(f"- `{ts}` **{err}** → {outcome} (conf {int(float(conf) * 100)}%)")
    return "\n".join(lines)


HELP_TEXT = (
    "**`/aiops` — AIOps agent commands**\n"
    "- `/aiops status` — agent + dependencies status\n"
    "- `/aiops incidents [N]` — last N incidents (default 5, max 20)\n"
    "- `/aiops help` — this help\n"
)


async def _process_alert_with_diagnosis(
    alert: AlertItem,
    http_client: httpx.AsyncClient,
    chroma_client,
    redis_client=None,
) -> None:
    """Background task: RAG query → retrieve context → LLM diagnosis → Mattermost."""
    severity, alert_name, pod, namespace = _extract_alert_meta(alert)
    is_chaos = namespace == "arturo-chaos"
    t_pipeline_start = time.time()
    chaos_outcome = "pipeline_failed"
    _llm_timeout = False
    dedup_key: tuple[str, str, str] | None = (alert_name, namespace, pod)
    try:
        description = alert.annotations.get("description", "")
        query = build_rag_query(alert.labels, description)

        # RAG retrieval (fail-open: empty context if ChromaDB down).
        # Lazy reconnect: a cached client goes stale if the ChromaDB StatefulSet pod
        # restarts after the agent started. On failure we discard it, build a fresh
        # client once, and persist it back so status + future alerts + incident queries
        # recover without restarting the agent.
        rag_degraded = False
        try:
            rag_context = await retrieve_context(query, http_client, chroma_client)
            DIAGNOSIS_COUNTER.labels(outcome="rag_ok").inc()
        except Exception as exc:
            logger.warning("RAG retrieval failed for %s, attempting ChromaDB reconnect: %r", alert_name, exc)
            try:
                chroma_client = get_chroma_client()
                rag_context = await retrieve_context(query, http_client, chroma_client)
                app.state.chroma_client = chroma_client
                DIAGNOSIS_COUNTER.labels(outcome="rag_reconnect").inc()
                logger.info("ChromaDB reconnect succeeded for %s", alert_name)
            except Exception as exc2:
                logger.warning("RAG reconnect failed for %s, proceeding without context: %r", alert_name, exc2)
                rag_context = {"runbooks": [], "incidents": [], "query": query}
                rag_degraded = True
                DIAGNOSIS_COUNTER.labels(outcome="rag_failed").inc()

        # LLM diagnosis (fail-open: None if Ollama down or timeout)
        try:
            diagnosis = await generate_diagnosis(
                alert.labels, alert.annotations, alert.status,
                rag_context, http_client,
            )
            DIAGNOSIS_COUNTER.labels(outcome="success").inc()
        except httpx.TimeoutException as exc:
            logger.warning("Diagnosis timed out for %s (HTTP_TIMEOUT=%ss): %r", alert_name, int(settings.http_timeout), exc)
            diagnosis = None
            _llm_timeout = True
            DIAGNOSIS_COUNTER.labels(outcome="llm_timeout").inc()
        except Exception as exc:
            logger.warning("Diagnosis generation failed for %s: %r", alert_name, exc)
            diagnosis = None
            DIAGNOSIS_COUNTER.labels(outcome="llm_error").inc()

        remediation_result = None
        if diagnosis is not None:
            try:
                remediation_result = await process_remediation(diagnosis, rag_degraded=rag_degraded)
                REMEDIATION_COUNTER.labels(action=remediation_result["action"].value).inc()
            except Exception as exc:
                logger.warning("Remediation processing failed for %s: %r", alert_name, exc)
                REMEDIATION_COUNTER.labels(action="skipped").inc()

        # Rollback scheduling: if AUTO_REMEDIATE executed at least one command, schedule health check
        if (
            settings.remediation_rollback_enabled
            and remediation_result is not None
            and remediation_result["action"] == RemediationAction.AUTO_REMEDIATE
            and any(r.success for r in remediation_result.get("execute_results", []))
        ):
            snapshot: PrePatchSnapshot | None = remediation_result.get("pre_patch_snapshot")
            if snapshot is not None:
                incident_id = str(uuid.uuid4())
                await _schedule_rollback_evaluation(incident_id, snapshot, alert, diagnosis)
            else:
                ROLLBACK_COUNTER.labels(outcome="skipped_no_snapshot").inc()

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

        chaos_outcome = remediation_result["action"].value if remediation_result else "no_diagnosis"

        # Escalations with approvable commands → persist in Redis + send with interactive buttons
        if (
            remediation_result is not None
            and remediation_result["action"] == RemediationAction.ESCALATE
            and remediation_result.get("safe_commands")
        ):
            incident_id = str(uuid.uuid4())
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=ESCALATION_TTL_MINUTES)
            esc = PendingEscalation(
                incident_id=incident_id,
                alert_item=alert,
                diagnosis=diagnosis,
                safe_commands=remediation_result["safe_commands"],
                expires_at=expires_at,
            )
            ttl_seconds = ESCALATION_TTL_MINUTES * 60
            stored = False
            if redis_client is not None:
                stored = await store_escalation(incident_id, _escalation_to_dict(esc), ttl_seconds, redis_client)
            if stored:
                ESCALATION_STORE_COUNTER.labels(outcome="stored").inc()
                logger.info(
                    "Stored pending escalation %s for %s (%d commands) in Redis",
                    incident_id, alert_name, len(remediation_result["safe_commands"]),
                )
                await send_escalation_with_buttons(
                    header=_format_escalation_header(alert),
                    attachment_text=_format_escalation_body(diagnosis, remediation_result),
                    incident_id=incident_id,
                    callback_base_url=settings.agent_callback_url,
                    webhook_secret=settings.webhook_secret,
                )
            else:
                # Redis unavailable — send degraded message without buttons
                ESCALATION_STORE_COUNTER.labels(outcome="redis_down").inc()
                logger.warning(
                    "Redis unavailable — sending escalation without buttons for %s", alert_name,
                )
                msg = _format_diagnosis_message(alert, diagnosis, remediation_result)
                await send_mattermost_alert(msg + "\n\n_⚠️ Botones interactivos no disponibles (Redis caído)_")
        else:
            msg = _format_diagnosis_message(alert, diagnosis, remediation_result, llm_timeout=_llm_timeout)
            await send_mattermost_alert(msg)

    except Exception as exc:
        # Last resort: send raw alert
        logger.error("Full diagnosis pipeline failed for %s: %s", alert_name, exc)
        DIAGNOSIS_COUNTER.labels(outcome="pipeline_failed").inc()
        icon = "🔴" if alert.status == "firing" else "🟢"
        severity, alert_name, pod, namespace = _extract_alert_meta(alert)
        desc = alert.annotations.get("description", "Sin descripcion")
        fallback = f"{icon} **[{severity}] {alert_name}** en Pod `{pod}` (NS: `{namespace}`)\n> {desc}"
        try:
            await send_mattermost_alert(fallback)
        except Exception as fallback_exc:
            logger.error("Fallback Mattermost send also failed for %s: %s", alert_name, fallback_exc)
    finally:
        # Remove dedup key so future occurrences of the same alert can be processed
        if dedup_key is not None:
            async with _INFLIGHT_LOCK:
                IN_FLIGHT_ALERTS.discard(dedup_key)
        if is_chaos:
            try:
                starts_at_epoch = datetime.fromisoformat(alert.startsAt.replace("Z", "+00:00")).timestamp()
                mttd = max(0.0, t_pipeline_start - starts_at_epoch)
                mttr = max(0.0, time.time() - starts_at_epoch)
                CHAOS_EXPERIMENT_COUNTER.labels(experiment=alert_name, outcome=chaos_outcome).inc()
                CHAOS_MTTD_HISTOGRAM.labels(experiment=alert_name).observe(mttd)
                CHAOS_MTTR_HISTOGRAM.labels(experiment=alert_name).observe(mttr)
                logger.info(
                    "Chaos metrics recorded",
                    extra={"experiment": alert_name, "mttd_s": round(mttd, 1), "mttr_s": round(mttr, 1), "outcome": chaos_outcome},
                )
            except Exception as chaos_exc:
                logger.warning("Failed to record chaos metrics for %s: %s", alert_name, chaos_exc)


async def _handle_stream_entry(entry_id: str, fields: dict) -> None:
    """
    Handler del consumidor de la cola (F2): decodifica una entrada del stream y
    delega en el pipeline existente, sin tocarlo. Lo invoca `consume_loop`.

    Lanza si la decodificación o el pipeline fallan → `consume_loop` no hace XACK
    y la entrada queda pendiente para reclaim (Slice 2).
    """
    alert = AlertItem.model_validate_json(fields["payload"])
    try:
        await _process_alert_with_diagnosis(
            alert, app.state.http_client, app.state.chroma_client, app.state.redis,
        )
        QUEUE_PROCESSED.labels(outcome="success").inc()
    except Exception:
        QUEUE_PROCESSED.labels(outcome="error").inc()
        raise


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
        _, alert_name, _, _ = _extract_alert_meta(alert)

        logger.info(
            "Processing alert %d/%d",
            idx + 1, len(payload.alerts),
            extra={
                "alertname": alert_name,
                "firing_status": alert.status,
            },
        )

        if alert.status == "firing":
            if settings.queue_enabled and app.state.redis is not None:
                # Camino de cola (F2): encolar en Redis Streams. Fail-closed —
                # si XADD falla devolvemos 503 y Alertmanager reintenta (no perder alertas).
                _, _, pod, namespace = _extract_alert_meta(alert)
                fingerprint = f"{alert_name}:{namespace}:{pod}"
                try:
                    entry_id = await enqueue_alert(
                        app.state.redis, alert.model_dump_json(), fingerprint,
                    )
                except Exception as exc:
                    logger.error("Failed to enqueue alert %s: %r", alert_name, exc)
                    WEBHOOK_COUNTER.labels(status="error").inc()
                    raise HTTPException(status_code=503, detail="alert queue unavailable")
                if entry_id is None:
                    logger.info(
                        "Duplicate alert skipped (dedup window)",
                        extra={"alertname": alert_name, "fingerprint": fingerprint},
                    )
                    DEDUP_COUNTER.labels(alertname=alert_name).inc()
                continue

            # Camino legacy (BackgroundTasks in-process) — default mientras QUEUE_ENABLED=False.
            # Dedup in-flight: skip if the same (alertname, namespace, pod) is already processing
            dedup_key = (alert_name, alert.labels.get("namespace", ""), alert.labels.get("pod", ""))
            async with _INFLIGHT_LOCK:
                if dedup_key in IN_FLIGHT_ALERTS:
                    logger.info(
                        "Duplicate in-flight alert skipped",
                        extra={"alertname": alert_name, "dedup_key": str(dedup_key)},
                    )
                    DEDUP_COUNTER.labels(alertname=alert_name).inc()
                    continue
                IN_FLIGHT_ALERTS.add(dedup_key)
            # Full RAG + diagnosis pipeline for firing alerts
            background_tasks.add_task(
                _process_alert_with_diagnosis,
                alert, app.state.http_client, app.state.chroma_client, app.state.redis,
            )
        else:
            # Resolved alerts: simple notification, no diagnosis needed
            severity, _, pod, namespace = _extract_alert_meta(alert)
            msg = f"🟢 **[RESOLVED] [{severity}] {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"
            background_tasks.add_task(send_mattermost_alert, msg)
        
    WEBHOOK_COUNTER.labels(status="success").inc()
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
    if not _verify_hmac_token(
        payload.context.incident_id, payload.context.action, payload.context.hmac_token
    ):
        raise HTTPException(status_code=401, detail="Invalid callback token")

    redis_client = app.state.redis
    incident_data = await get_escalation(payload.context.incident_id, redis_client) if redis_client else None
    if incident_data is None:
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
    try:
        incident = _dict_to_escalation(incident_data)
    except Exception as exc:
        logger.error("Failed to deserialize escalation %s: %s", payload.context.incident_id, exc)
        return {
            "ephemeral_text": "Error interno al recuperar la escalación.",
            "update": {"message": "⚠️ Error interno — no se pudo procesar la respuesta.", "props": {"attachments": []}},
        }
    if datetime.now(timezone.utc) > incident.expires_at:
        await delete_escalation(payload.context.incident_id, redis_client)
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
    await delete_escalation(payload.context.incident_id, redis_client)

    user = payload.user_name or "human"
    action = payload.context.action

    if action == "approve":
        try:
            execute_results = await execute_commands(incident.safe_commands)
            log = results_to_log(execute_results)
            REMEDIATION_COUNTER.labels(action="human_approved").inc()
        except Exception as exc:
            logger.error("Command execution failed for incident %s: %s", incident.incident_id, exc)
            log = f"ERROR: {exc}"
            REMEDIATION_COUNTER.labels(action="human_approve_failed").inc()
        decision_line = f"\n---\n✅ **Remediación APROBADA** por @{user}\n```\n{log}\n```"
        remediation_for_feedback = {
            "action": RemediationAction.AUTO_REMEDIATE,
            "execution_log": log,
            "safe_commands": incident.safe_commands,
            "blocked_commands": [],
        }
        logger.info("Human approved remediation for incident %s", incident.incident_id)
    elif action == "reject":
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
    else:
        logger.warning(
            "Unknown action in callback",
            extra={"action": action, "incident_id": incident.incident_id},
        )
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")

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
    "/webhook/command",
    summary="Recibe slash commands de Mattermost (/aiops ...)",
)
async def handle_slash_command(
    token: Annotated[str | None, Form()] = None,
    command: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    user_name: Annotated[str | None, Form()] = None,
) -> dict:
    """
    Atiende el slash command /aiops de Mattermost.
    MM envía application/x-www-form-urlencoded. Responde ephemeral (solo el invocador lo ve).
    Auth: MM_COMMAND_TOKEN static shared secret — fail-open si no configurado (dev/test).
    """
    if settings.mm_command_token:
        if not token or not hmac.compare_digest(token, settings.mm_command_token):
            raise HTTPException(status_code=401, detail="Invalid command token")

    if command != "/aiops":
        return {"response_type": "ephemeral", "text": f"Unknown command: `{command}`"}

    parts = text.strip().split()
    sub = parts[0].lower() if parts else "help"

    if sub == "status":
        ollama_ok = False
        try:
            r = await app.state.http_client.get(settings.ollama_tags, timeout=settings.health_timeout)
            ollama_ok = r.status_code == 200
        except Exception:
            pass
        pending_count = await count_escalations(app.state.redis) if app.state.redis else 0
        text_out = _format_status_response(ollama_ok, app.state.chroma_client is not None, pending_count)

    elif sub == "incidents":
        limit = 5
        if len(parts) > 1:
            try:
                limit = max(1, min(20, int(parts[1])))
            except ValueError:
                return {
                    "response_type": "ephemeral",
                    "text": f"Invalid N: `{parts[1]}` (expected integer 1–20)",
                }
        rows = _query_recent_incidents(app.state.chroma_client, limit)
        text_out = _format_incidents_response(rows)

    elif sub == "help":
        text_out = HELP_TEXT

    else:
        text_out = f"Unknown subcommand: `{sub}`\n\n{HELP_TEXT}"

    logger.info(
        "Slash command processed",
        extra={"command": command, "subcommand": sub, "user": user_name},
    )
    return {"response_type": "ephemeral", "text": text_out}


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
    logger.info("Processing request", extra={"request_id": request_id, "message_preview": request.message[:100]})

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
            logger.error("Ollama HTTP error", extra={"request_id": request_id, "status_code": exc.response.status_code})
            raise HTTPException(
                status_code=502, detail=f"LLM returned error: {exc.response.status_code}"
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < settings.retry_max_attempts - 1:
                delay = backoff_delay(attempt, settings.retry_base_delay, settings.retry_max_delay)
                logger.warning(
                    "Ollama attempt failed, retrying",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_attempts": settings.retry_max_attempts,
                        "exception_type": type(exc).__name__,
                        "retry_delay_seconds": delay,
                    },
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Ollama failed after max attempts",
                    extra={
                        "request_id": request_id,
                        "max_attempts": settings.retry_max_attempts,
                        "error": str(exc),
                    },
                )
        except httpx.HTTPError as exc:
            logger.error("Ollama connection error", extra={"request_id": request_id, "error": str(exc)})
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
        logger.info("Extraction succeeded", extra={"request_id": request_id, "method": method, "duration_ms": duration_ms})
    else:
        logger.warning("Extraction failed", extra={"request_id": request_id, "duration_ms": duration_ms, "raw_preview": raw[:150]})

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
