"""
AIOps Infrastructure Agent — Fase 1

Extrae parámetros de infraestructura GCP a partir de mensajes en lenguaje
natural, usando un LLM local (Ollama) como motor de inferencia.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict, field
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
    build_incident_document, ingest_incident, runbook_filter_for_alert,
    make_incident_doc_id, incident_worth_ingesting, COLLECTION_INCIDENTS,
    INCIDENT_OUTCOME_PENDING, INCIDENT_OUTCOME_CURED,
    INCIDENT_OUTCOME_ROLLED_BACK, INCIDENT_OUTCOME_ROLLBACK_FAILED,
    INCIDENT_OUTCOME_RESOLVED_OBSERVED,
)
from diagnosis import generate_diagnosis
from remediation import (
    process_remediation, execute_commands, results_to_log, RemediationAction,
    capture_pre_patch_value, check_pod_health, revert_patch, PrePatchSnapshot,
    _limit_resource, seal_proposed_action, ground_confidence, is_structured_remediation,
    acquire_workload_cooldown, partition_by_permission, structured_command_variants,
)
from enrichment import gather_incident_context
from utils import backoff_delay
from escalation_store import store_escalation, get_escalation, delete_escalation, count_escalations
from incident_index import record_active_incident, pop_active_incident
from rollback_store import store_rollback, delete_rollback, list_rollbacks
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
    ["outcome"],  # "persisted" | "skipped" | "failed" | "verdict_failed"
)

# R2 (real learning loop): distribution of final rollback verdicts re-written into
# the incident collection — the narrable "did the auto-fix actually hold?" signal.
FEEDBACK_VERDICT_COUNTER = Counter(
    "aiops_feedback_verdict_total",
    "Incident outcomes re-upserted after the rollback verdict",
    ["outcome"],  # "cured" | "rolled_back" | "rollback_failed" | "resolved_observed"
)

# R5 (observational loop): the "did it ever clear, and how long did it take?" signal.
# Fed by Alertmanager `resolved` alerts correlated back to their firing incident by
# fingerprint. Closes the loop for EVERY alert class, including the non-auto ones the
# rollback verdict never sees (escalated / suggested-only).
INCIDENT_RESOLUTION_COUNTER = Counter(
    "aiops_incident_resolution_total",
    "Resolved alerts correlated back to a firing incident",
    ["correlated"],  # "hit" (matched an active incident) | "miss" (no active incident)
)
INCIDENT_RESOLUTION_HISTOGRAM = Histogram(
    "aiops_incident_resolution_seconds",
    "Seconds from incident ingest to the matching Alertmanager `resolved` alert",
    ["error_class"],
    buckets=[60, 180, 300, 600, 1800, 3600],
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
ENRICHMENT_COUNTER = Counter(
    "aiops_enrichment_total",
    "Cluster grounding (enrichment) gather outcomes per alert",
    ["outcome"],  # "gathered" | "workload_unresolved" | "skipped" | "error"
)


# ── Human Escalation State ────────────────────────────────────────────────────

# C-03: sourced from Settings (env var ESCALATION_TTL_MINUTES) so demo/absence windows
# don't require a rebuild. Module alias kept for readability at the 3 call sites.
ESCALATION_TTL_MINUTES = settings.escalation_ttl_minutes


@dataclass
class PendingEscalation:
    incident_id: str
    alert_item: AlertItem
    diagnosis: dict
    safe_commands: list[str]
    expires_at: datetime
    # R2 human/auto parity: the ChromaDB incident doc_id computed at diagnosis time.
    # Carried through Redis so the human approve path can re-upsert the SAME document
    # with the rollback verdict (make_incident_doc_id embeds time.time() → not
    # reproducible at approve time, minutes later). Empty for pre-R2 escalations.
    incident_doc_id: str = ""
    # C-08: callback-action → engine-built command for each approve variant
    # ({"approve_engine": ..., "approve_model": ...} for a two-button choice, or
    # {"approve": ...} for the single-button case). The approve handler runs the command
    # keyed by the clicked action; empty for pre-C-08 escalations → falls back to safe_commands.
    command_variants: dict[str, str] = field(default_factory=dict)


# ── Rollback State ────────────────────────────────────────────────────────────

@dataclass
class RollbackContext:
    incident_id: str
    snapshot: PrePatchSnapshot
    alert_item: AlertItem
    diagnosis: dict
    scheduled_at: datetime
    # R2 (real learning loop): the ChromaDB incident doc_id + a serializable
    # remediation summary, so the rollback verdict can re-upsert the same document
    # with the true outcome (cured / rolled_back). Defaulted for back-compat with
    # contexts persisted before R2.
    doc_id: str = ""
    remediation: dict = field(default_factory=dict)


IN_FLIGHT_ROLLBACKS: dict[str, RollbackContext] = {}
_ROLLBACK_LOCK = asyncio.Lock()


def _verify_hmac_token(incident_id: str, action: str, token: str | None) -> bool:
    """Returns True if the HMAC token is valid, or if auth is safely disabled.

    Fail-open (no webhook_secret configured) is allowed ONLY in dry-run mode
    (dev/test). With real execution (remediation_dry_run=False) a missing secret
    fails CLOSED: an unauthenticated "approve" callback would trigger a real
    remediation. See P0·2 (auditoría 2026-07-01).
    """
    if not settings.webhook_secret:
        return settings.remediation_dry_run
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
        "incident_doc_id": esc.incident_doc_id,
        "command_variants": esc.command_variants,
    }


def _dict_to_escalation(data: dict) -> PendingEscalation:
    """Deserialize a Redis-stored dict back to PendingEscalation."""
    return PendingEscalation(
        incident_id=data["incident_id"],
        alert_item=AlertItem(**data["alert_item"]),
        diagnosis=data["diagnosis"],
        safe_commands=data["safe_commands"],
        expires_at=datetime.fromisoformat(data["expires_at"]),
        incident_doc_id=data.get("incident_doc_id", ""),
        command_variants=data.get("command_variants", {}),
    )


def _remediation_summary(remediation_result: dict | None) -> dict:
    """JSON-serializable subset of a remediation result, enough to regenerate the
    incident document's text at rollback-verdict time (R2). Excludes the action
    (the verdict outcome supersedes it) and non-serializable objects (enums,
    dataclasses) so it survives the Redis round-trip."""
    if not remediation_result:
        return {}
    return {
        "execution_log": remediation_result.get("execution_log", ""),
        "safe_commands": list(remediation_result.get("safe_commands", [])),
    }


async def _reupsert_incident_outcome(ctx: RollbackContext, outcome: str) -> None:
    """R2 (real learning loop): re-write the incident document with the final rollback
    verdict so retrieval reflects reality — a rolled-back fix stops being a positive
    precedent. Reuses ctx.doc_id (shared with the diagnosis-time ingest) so the upsert
    updates the same ChromaDB document in place. Fail-open: a persistence error never
    affects the rollback outcome already applied."""
    if not ctx.doc_id:
        return
    try:
        alert_data = {
            "labels": ctx.alert_item.labels,
            "annotations": ctx.alert_item.annotations,
        }
        doc_id, text, metadata = build_incident_document(
            alert_data, ctx.diagnosis, ctx.remediation,
            doc_id=ctx.doc_id, outcome=outcome,
        )
        await ingest_incident(
            doc_id=doc_id, text=text, metadata=metadata,
            http_client=app.state.http_client,
            chroma_client=app.state.chroma_client,
        )
        FEEDBACK_VERDICT_COUNTER.labels(outcome=outcome).inc()
    except Exception as exc:
        logger.warning("Failed to re-upsert incident verdict %s: %s", ctx.doc_id, exc)
        FEEDBACK_COUNTER.labels(outcome="verdict_failed").inc()


async def _correlate_resolution(fingerprint: str) -> None:
    """R5 (observational loop): an Alertmanager `resolved` alert arrived — close the
    matching active incident. Always emits the resolution-time metric on a hit; for
    incidents the auto rollback loop does NOT own (awaits_verdict=False) it also
    re-upserts the ChromaDB doc as resolved_observed. Fail-open: any error is swallowed
    so the resolved-alert webhook is never affected."""
    record = await pop_active_incident(fingerprint, app.state.redis)
    if record is None:
        INCIDENT_RESOLUTION_COUNTER.labels(correlated="miss").inc()
        return
    INCIDENT_RESOLUTION_COUNTER.labels(correlated="hit").inc()

    error_class = record.get("error_class", "unknown")
    elapsed: float | None
    try:
        elapsed = max(0.0, time.time() - float(record.get("started_at", 0)))
        INCIDENT_RESOLUTION_HISTOGRAM.labels(error_class=error_class).observe(elapsed)
    except (TypeError, ValueError):
        elapsed = None

    # The rollback verdict owns the outcome for auto-remediated incidents (cured /
    # rolled_back). A weaker observational signal must never overwrite it, so R5 only
    # writes an outcome for incidents that never entered that loop.
    if record.get("awaits_verdict"):
        return
    doc_id = record.get("doc_id")
    metadata = record.get("metadata")
    text = record.get("text")
    if not (doc_id and metadata and text):
        return
    try:
        metadata = {**metadata, "outcome": INCIDENT_OUTCOME_RESOLVED_OBSERVED}
        note = (
            f"Resolution: alert cleared after {int(elapsed)}s (observed via Alertmanager resolved)"
            if elapsed is not None
            else "Resolution: alert cleared (observed via Alertmanager resolved)"
        )
        await ingest_incident(
            doc_id=doc_id, text=f"{text}\n{note}", metadata=metadata,
            http_client=app.state.http_client,
            chroma_client=app.state.chroma_client,
        )
        FEEDBACK_VERDICT_COUNTER.labels(outcome=INCIDENT_OUTCOME_RESOLVED_OBSERVED).inc()
    except Exception as exc:
        logger.warning("Failed to re-upsert resolved incident %s: %s", doc_id, exc)
        FEEDBACK_COUNTER.labels(outcome="verdict_failed").inc()


def _rollback_to_dict(ctx: RollbackContext) -> dict:
    """Serialize a RollbackContext to a JSON-serializable dict for Redis storage."""
    return {
        "incident_id": ctx.incident_id,
        "snapshot": asdict(ctx.snapshot),
        "alert_item": ctx.alert_item.model_dump(mode="json"),
        "diagnosis": ctx.diagnosis,
        "scheduled_at": ctx.scheduled_at.isoformat(),
        "doc_id": ctx.doc_id,
        "remediation": ctx.remediation,
    }


def _dict_to_rollback(data: dict) -> RollbackContext:
    """Deserialize a Redis-stored dict back to a RollbackContext."""
    return RollbackContext(
        incident_id=data["incident_id"],
        snapshot=PrePatchSnapshot(**data["snapshot"]),
        alert_item=AlertItem(**data["alert_item"]),
        diagnosis=data["diagnosis"],
        scheduled_at=datetime.fromisoformat(data["scheduled_at"]),
        doc_id=data.get("doc_id", ""),
        remediation=data.get("remediation", {}),
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
    doc_id: str = "",
    remediation: dict | None = None,
) -> None:
    """Register a rollback context and schedule the evaluation task."""
    ctx = RollbackContext(
        incident_id=incident_id,
        snapshot=snapshot,
        alert_item=alert_item,
        diagnosis=diagnosis,
        scheduled_at=datetime.now(timezone.utc),
        doc_id=doc_id,
        remediation=remediation or {},
    )
    async with _ROLLBACK_LOCK:
        IN_FLIGHT_ROLLBACKS[incident_id] = ctx
    # Durability backstop (P0·3): persist so a pod restart during the wait window
    # doesn't strand an un-reverted patch. TTL outlives the wait + evaluation.
    if getattr(app.state, "redis", None):
        ttl = settings.remediation_rollback_timeout * 2 + settings.remediation_rollback_grace
        await store_rollback(incident_id, _rollback_to_dict(ctx), ttl, app.state.redis)
    ROLLBACK_COUNTER.labels(outcome="scheduled").inc()
    logger.info(
        "Rollback evaluation scheduled",
        extra={"incident_id": incident_id, "timeout_s": settings.remediation_rollback_timeout},
    )
    asyncio.create_task(_evaluate_rollback(incident_id))


async def _evaluate_rollback(incident_id: str) -> None:
    """Wait until the rollback deadline, then check pod health and revert if needed."""
    try:
        async with _ROLLBACK_LOCK:
            ctx = IN_FLIGHT_ROLLBACKS.get(incident_id)

        if ctx is None:
            logger.info("Rollback context already cleared", extra={"incident_id": incident_id})
            return

        # Sleep only the time remaining until the deadline. On the happy path this
        # is the full timeout; for a context recovered from Redis after a restart
        # (P0·3) it is what's left — or 0 if the deadline already passed.
        elapsed = (datetime.now(timezone.utc) - ctx.scheduled_at).total_seconds()
        remaining = settings.remediation_rollback_timeout - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

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
            await _reupsert_incident_outcome(ctx, INCIDENT_OUTCOME_CURED)
            msg = (
                f"✅ **Remediation healthy** — `{alert_name}`\n"
                f"Pod(s) running normally {settings.remediation_rollback_timeout}s after patch. "
                f"No rollback needed.\n"
                f"_Deployment: `{ctx.snapshot.deployment}` · NS: `{ctx.snapshot.namespace}`_"
            )
            await send_mattermost_alert(msg)
        else:
            result = await revert_patch(ctx.snapshot)
            resource = _limit_resource(ctx.snapshot.field)
            if result.success:
                ROLLBACK_COUNTER.labels(outcome="reverted").inc()
                await _reupsert_incident_outcome(ctx, INCIDENT_OUTCOME_ROLLED_BACK)
                msg = (
                    f"🔄 **Rollback executed** — `{alert_name}`\n"
                    f"Pod(s) still failing after {settings.remediation_rollback_timeout}s "
                    f"(reason: `{health.reason}`). "
                    f"Reverted `{ctx.snapshot.deployment}` {resource} to `{ctx.snapshot.value}`.\n"
                    f"```\n{results_to_log([result])}\n```"
                )
            else:
                ROLLBACK_COUNTER.labels(outcome="revert_failed").inc()
                await _reupsert_incident_outcome(ctx, INCIDENT_OUTCOME_ROLLBACK_FAILED)
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
        if getattr(app.state, "redis", None):
            await delete_rollback(incident_id, app.state.redis)


async def _recover_rollbacks() -> None:
    """Startup recovery (P0·3): re-arm rollback evaluations persisted in Redis.

    If the pod restarted during a rollback wait window, the in-memory dict and
    its sleeping task were lost — but the context survived in Redis. Re-register
    each and spawn evaluation (which sleeps only the *remaining* time, or fires
    immediately if the deadline already passed).
    """
    redis = getattr(app.state, "redis", None)
    if not redis:
        return
    for data in await list_rollbacks(redis):
        try:
            ctx = _dict_to_rollback(data)
        except Exception as exc:
            logger.warning("Skipping unparseable persisted rollback: %s", exc)
            continue
        async with _ROLLBACK_LOCK:
            if ctx.incident_id in IN_FLIGHT_ROLLBACKS:
                continue
            IN_FLIGHT_ROLLBACKS[ctx.incident_id] = ctx
        logger.info(
            "Recovered in-flight rollback from Redis", extra={"incident_id": ctx.incident_id}
        )
        asyncio.create_task(_evaluate_rollback(ctx.incident_id))


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
        # ensure_collections is blocking HTTP; offload it so startup never blocks the
        # event loop (F-03). Not a request hot path, but keeps the pattern consistent.
        await asyncio.to_thread(ensure_collections, app.state.chroma_client)
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

    # Redis Streams consumer (F2) — la cola es el camino único de ingesta.
    app.state.consumer_task = None
    app.state.reclaim_task = None
    if app.state.redis is not None:
        try:
            await ensure_group(app.state.redis)
            app.state.consumer_task = asyncio.create_task(
                consume_loop(app.state.redis, _handle_stream_entry)
            )
            app.state.reclaim_task = asyncio.create_task(_periodic_reclaim())
            logger.info("Stream consumer + reclaim started")
        except Exception as exc:
            logger.error("Failed to start stream consumer: %r", exc)
    else:
        logger.error("Redis unavailable at startup — ingestion queue cannot start (readyz will report 503)")

    # Durability backstop (P0·3): re-arm any rollback that was mid-wait when the
    # pod last stopped, so an un-reverted patch doesn't get stranded.
    await _recover_rollbacks()

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
        if settings.remediation_dry_run:
            logger.warning(
                "WEBHOOK_SECRET not configured — button callbacks fail-open (dry-run mode)"
            )
        else:
            logger.error(
                "WEBHOOK_SECRET not configured with REMEDIATION_DRY_RUN=false — "
                "button callbacks will be REJECTED (fail-closed). Escalations cannot be approved."
            )

    if not settings.mm_command_token:
        if settings.remediation_dry_run:
            logger.warning(
                "MM_COMMAND_TOKEN not configured — /aiops accepts any token (dry-run mode)"
            )
        else:
            logger.error(
                "MM_COMMAND_TOKEN not configured with REMEDIATION_DRY_RUN=false — "
                "/aiops slash command will be REJECTED (fail-closed)."
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
    for _outcome in ("gathered", "workload_unresolved", "skipped", "error"):
        ENRICHMENT_COUNTER.labels(outcome=_outcome).inc(0)

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


@app.get("/readyz", summary="Readiness probe — Redis (cola de ingesta)")
async def readyz():
    """
    Readiness probe: la dependencia crítica de ingesta es Redis (el webhook encola
    ahí). Se chequea solo Redis. Ollama lento/caído NO debe sacar al pod de rotación
    — la cola está para bufferear esa lentitud; procesar es problema del worker, no
    del ingress.

    Devuelve 503 si Redis no está sano.
    Kubernetes usa este endpoint para decidir si enrutar tráfico al pod.
    """
    redis_client = getattr(app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable (queue mode)")
    try:
        await asyncio.wait_for(redis_client.ping(), timeout=settings.health_timeout)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {exc}")
    return {"status": "ready", "mode": "queue", "redis": "up"}


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


def _alert_fingerprint(alert: AlertItem) -> str:
    """Correlation key shared by the firing (enqueue dedup + R5 index) and resolved
    (R5 close-out) paths. Single source so the two sides never diverge — a mismatch
    would silently break resolution correlation."""
    _, alert_name, pod, namespace = _extract_alert_meta(alert)
    return f"{alert_name}:{namespace}:{pod}"


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

    # Grounded confidence (v2 Eje A): make the grounding visible — when the cluster
    # signal overrode the model's self-assessment, show both.
    conf_line = f"**Risk:** {risk} | **Confidence:** {confidence_pct}%"
    model_conf = diagnosis.get("model_confidence")
    if isinstance(model_conf, (int, float)):
        conf_line += f" _(grounded del cluster; el modelo dijo {int(model_conf * 100)}%)_"

    parts = [
        header,
        f"\n**Diagnosis:** {diagnosis.get('diagnosis', 'Sin diagnóstico')}",
        conf_line,
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
        if action != RemediationAction.AUTO_REMEDIATE and remediation.get("structured_command"):
            parts.append(
                f"🔧 **Fix determinista (motor):**\n```\n{remediation['structured_command']}\n```"
            )

    return "\n".join(parts)


def _format_escalation_header(alert: AlertItem) -> str:
    """Línea de título para el mensaje de escalación con botones."""
    severity, alert_name, pod, namespace = _extract_alert_meta(alert)
    return f"⚠️ **[{severity}] ESCALATION REQUIRED — {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"


def _variant_button_label(variant: dict) -> str:
    """Mattermost button label for a C-08 approve variant."""
    if variant["id"] == "engine":
        return f"✅ ×2 motor ({variant['value']})"
    return f"⚠️ Valor modelo ({variant['value']})"


def _variant_body_label(variant: dict) -> str:
    """Attachment-body heading for a C-08 approve variant."""
    if variant["id"] == "engine":
        return "**Opción A — ×2 determinista del motor (recomendado, requiere aprobación):**"
    return "**Opción B — valor propuesto por el modelo (requiere aprobación):**"


def _format_escalation_body(
    diagnosis: dict,
    remediation: dict,
    denied_commands: list[str] | None = None,
    command_variants: list[dict] | None = None,
) -> str:
    """Cuerpo del attachment (sin botones) para el mensaje de escalación.

    C-07: los comandos que RBAC deniega (denied_commands) se muestran como *sugerencias*
    (el agente no puede ejecutarlos) y quedan fuera del set aprobable — evita ofrecer un
    botón que ejecutaría-y-fallaría (`kubectl top` Forbidden bajo least-privilege).

    C-08: con dos `command_variants` (valor del modelo vs ×2 del motor) el cuerpo muestra un
    bloque etiquetado por variante para que el humano lea qué ejecuta cada botón.
    """
    denied_commands = denied_commands or []
    confidence_pct = int(diagnosis.get("confidence", 0.0) * 100)
    risk = diagnosis.get("risk", "high").upper()

    conf_line = f"**Risk:** {risk} | **Confidence:** {confidence_pct}%"
    model_conf = diagnosis.get("model_confidence")
    if isinstance(model_conf, (int, float)):
        conf_line += f" _(grounded del cluster; el modelo dijo {int(model_conf * 100)}%)_"

    parts = [
        f"**Diagnóstico:** {diagnosis.get('diagnosis', 'Sin diagnóstico')}",
        conf_line,
    ]

    if command_variants and len(command_variants) > 1:
        # C-08: two engine-built candidates — the human arbitrates model vs. deterministic ×2.
        for v in command_variants:
            parts.append(f"{_variant_body_label(v)}\n```\n{v['command']}\n```")
    else:
        structured_cmd = remediation.get("structured_command")
        if structured_cmd:
            approvable = [structured_cmd]
        else:
            approvable = [c for c in remediation.get("safe_commands", []) if c not in denied_commands]
        if approvable:
            cmds_str = "\n".join(approvable)
            label = (
                "**Comando determinista del motor (requiere aprobación):**"
                if structured_cmd else "**Comandos propuestos (requieren aprobación):**"
            )
            parts.append(f"{label}\n```\n{cmds_str}\n```")

    if denied_commands:
        denied_str = "\n".join(denied_commands)
        parts.append(
            "**Comandos sugeridos (el agente no tiene permisos para ejecutarlos):**\n"
            f"```\n{denied_str}\n```"
        )

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
    try:
        description = alert.annotations.get("description", "")
        query = build_rag_query(alert.labels, description)
        # R1: filter the runbook query by error_class (alertname → class, deterministic).
        # The retrieval falls back to an unfiltered semantic query if the filter misses.
        runbook_filter = runbook_filter_for_alert(alert.labels)

        # RAG retrieval (fail-open: empty context if ChromaDB down).
        # Lazy reconnect: a cached client goes stale if the ChromaDB StatefulSet pod
        # restarts after the agent started. On failure we discard it, build a fresh
        # client once, and persist it back so status + future alerts + incident queries
        # recover without restarting the agent.
        rag_degraded = False
        try:
            rag_context = await retrieve_context(query, http_client, chroma_client, metadata_filter=runbook_filter)
            DIAGNOSIS_COUNTER.labels(outcome="rag_ok").inc()
        except Exception as exc:
            logger.warning("RAG retrieval failed for %s, attempting ChromaDB reconnect: %r", alert_name, exc)
            try:
                chroma_client = get_chroma_client()
                rag_context = await retrieve_context(query, http_client, chroma_client, metadata_filter=runbook_filter)
                app.state.chroma_client = chroma_client
                DIAGNOSIS_COUNTER.labels(outcome="rag_reconnect").inc()
                logger.info("ChromaDB reconnect succeeded for %s", alert_name)
            except Exception as exc2:
                logger.warning("RAG reconnect failed for %s, proceeding without context: %r", alert_name, exc2)
                rag_context = {"runbooks": [], "incidents": [], "query": query}
                rag_degraded = True
                DIAGNOSIS_COUNTER.labels(outcome="rag_failed").inc()

        # Grounding (v2 Eje A): gather the cluster snapshot ONCE, up front. It feeds the
        # diagnosis prompt (the model reasons over real facts) and then seals the action
        # target below. Fail-soft — a failed/disabled gather returns gather_ok=False.
        snapshot = None
        try:
            snapshot = await gather_incident_context(alert.labels)
        except Exception as exc:
            logger.warning("Enrichment gather failed for %s, proceeding LLM-only: %r", alert_name, exc)
            ENRICHMENT_COUNTER.labels(outcome="error").inc()
        else:
            # Zero black-box: the grounding stage is observable per alert. "gathered" means
            # the pod snapshot AND the owning workload were confirmed from the cluster.
            if snapshot.gather_ok:
                _outcome = "gathered" if snapshot.workload_name else "workload_unresolved"
            else:
                _outcome = "skipped"
            ENRICHMENT_COUNTER.labels(outcome=_outcome).inc()

        # LLM diagnosis (fail-open: None if Ollama down or timeout)
        try:
            diagnosis = await generate_diagnosis(
                alert.labels, alert.annotations, alert.status,
                rag_context, http_client, snapshot=snapshot,
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
            # Grounding (v2 Eje A): seal the action target/current_value from the cluster
            # snapshot gathered above before deciding. Fail-soft — a failed/disabled gather
            # (snapshot is None or gather_ok=False) leaves the LLM values untouched.
            try:
                seal_proposed_action(diagnosis, snapshot)
                ground_confidence(diagnosis, snapshot)
            except Exception as exc:
                logger.warning("Seal/ground failed for %s, proceeding LLM-only: %r", alert_name, exc)
            try:
                remediation_result = await process_remediation(
                    diagnosis, rag_degraded=rag_degraded, redis_client=redis_client,
                )
                REMEDIATION_COUNTER.labels(action=remediation_result["action"].value).inc()
            except Exception as exc:
                logger.warning("Remediation processing failed for %s: %r", alert_name, exc)
                REMEDIATION_COUNTER.labels(action="skipped").inc()

        # R2 (real learning loop): one incident doc_id shared between the feedback-loop
        # ingest below and the rollback verdict re-upsert, so both write the same
        # ChromaDB document (the verdict updates the provisional outcome in place).
        incident_doc_id = (
            make_incident_doc_id(alert.labels.get("alertname", "UnknownAlert"))
            if diagnosis is not None else None
        )

        # Rollback scheduling: if AUTO_REMEDIATE executed at least one command, schedule health check
        rollback_scheduled = False
        if (
            settings.remediation_rollback_enabled
            and remediation_result is not None
            and remediation_result["action"] == RemediationAction.AUTO_REMEDIATE
            and any(r.success for r in remediation_result.get("execute_results", []))
        ):
            snapshot: PrePatchSnapshot | None = remediation_result.get("pre_patch_snapshot")
            if snapshot is not None:
                incident_id = str(uuid.uuid4())
                await _schedule_rollback_evaluation(
                    incident_id, snapshot, alert, diagnosis,
                    doc_id=incident_doc_id or "",
                    remediation=_remediation_summary(remediation_result),
                )
                rollback_scheduled = True
            else:
                ROLLBACK_COUNTER.labels(outcome="skipped_no_snapshot").inc()

        # Feedback loop: persist incident in ChromaDB (fail-open). R2: when a rollback
        # verdict is pending, the incident is provisional (auto_pending) — the verdict
        # re-upsert later flips it to cured/rolled_back so retrieval never cites an
        # unverified (or reverted) fix as a good precedent.
        if diagnosis is None:
            FEEDBACK_COUNTER.labels(outcome="skipped").inc()
        elif not incident_worth_ingesting(diagnosis):
            # E4 quality gate: an unparseable/zero-confidence diagnosis is not a
            # precedent — archiving it would contaminate future retrievals.
            logger.info("Skipping incident ingest for %s: zero-confidence diagnosis", alert_name)
            FEEDBACK_COUNTER.labels(outcome="skipped_low_quality").inc()
        else:
            try:
                alert_data = {"labels": alert.labels, "annotations": alert.annotations}
                doc_id, text, metadata = build_incident_document(
                    alert_data, diagnosis, remediation_result, doc_id=incident_doc_id,
                    outcome=INCIDENT_OUTCOME_PENDING if rollback_scheduled else None,
                )
                await ingest_incident(
                    doc_id=doc_id, text=text, metadata=metadata,
                    http_client=http_client, chroma_client=chroma_client,
                )
                FEEDBACK_COUNTER.labels(outcome="persisted").inc()
                # R5: index the active incident by fingerprint so a later Alertmanager
                # `resolved` can close it. awaits_verdict=rollback_scheduled → the
                # rollback loop owns the outcome (resolution only emits the metric,
                # never overwrites cured/rolled_back).
                await record_active_incident(
                    _alert_fingerprint(alert),
                    {
                        "doc_id": doc_id,
                        "error_class": metadata["error_class"],
                        "started_at": time.time(),
                        "awaits_verdict": rollback_scheduled,
                        "text": text,
                        "metadata": metadata,
                    },
                    settings.incident_correlation_ttl_seconds,
                    redis_client,
                )
            except Exception as exc:
                logger.warning("Failed to persist incident for %s: %s", alert_name, exc)
                FEEDBACK_COUNTER.labels(outcome="failed").inc()

        chaos_outcome = remediation_result["action"].value if remediation_result else "no_diagnosis"

        # Escalations with approvable commands → persist in Redis + send with interactive buttons.
        # For a structured remediation the approvable command IS the engine synthesis (single
        # source with the auto path — v2: what the human approves is exactly what runs), never
        # the model's free-text commands.
        structured_cmd = remediation_result.get("structured_command") if remediation_result else None
        # C-07: for a free-text escalation, pre-flight the commands with `auth can-i` — the
        # RBAC-denied ones (e.g. `kubectl top`, Forbidden under least-privilege) become
        # suggestions instead of approvable buttons that would execute-and-fail. The engine's
        # structured command is deterministic/known-good and skips the check.
        esc_commands: list[str] = []
        denied_cmds: list[str] = []
        # C-08: for a structured escalation, offer the human both engine-built candidates
        # (deterministic ×2 vs the model's value) as separate approve buttons when they
        # differ — the interactive "el modelo propone, el motor dispone". One button (as
        # before) when they coincide. variants carry the callback action for each button.
        variants: list[dict] = []
        command_variants: dict[str, str] = {}
        approve_variants: list[dict[str, str]] | None = None
        if remediation_result is not None and remediation_result["action"] == RemediationAction.ESCALATE:
            if structured_cmd:
                variants = structured_command_variants(diagnosis)
                if not variants:
                    # Belt-and-braces: process_remediation derives structured_cmd from this
                    # same sealed diagnosis, so variants can only be empty if the two ever
                    # diverge — keep the engine command approvable under the legacy action.
                    variants = [{"id": "model", "action": "approve", "value": "", "command": structured_cmd}]
                esc_commands = [v["command"] for v in variants]
                command_variants = {v["action"]: v["command"] for v in variants}
                if len(variants) > 1:
                    approve_variants = [
                        {"action": v["action"], "label": _variant_button_label(v)} for v in variants
                    ]
            elif remediation_result.get("safe_commands"):
                esc_commands, denied_cmds = await partition_by_permission(
                    remediation_result["safe_commands"], namespace,
                )

        if esc_commands:
            incident_id = str(uuid.uuid4())
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=ESCALATION_TTL_MINUTES)
            esc = PendingEscalation(
                incident_id=incident_id,
                alert_item=alert,
                diagnosis=diagnosis,
                safe_commands=esc_commands,
                expires_at=expires_at,
                incident_doc_id=incident_doc_id or "",
                command_variants=command_variants,
            )
            ttl_seconds = ESCALATION_TTL_MINUTES * 60
            stored = False
            if redis_client is not None:
                stored = await store_escalation(incident_id, _escalation_to_dict(esc), ttl_seconds, redis_client)
            if stored:
                ESCALATION_STORE_COUNTER.labels(outcome="stored").inc()
                logger.info(
                    "Stored pending escalation %s for %s (%d commands) in Redis",
                    incident_id, alert_name, len(esc_commands),
                )
                await send_escalation_with_buttons(
                    header=_format_escalation_header(alert),
                    attachment_text=_format_escalation_body(
                        diagnosis, remediation_result, denied_commands=denied_cmds,
                        command_variants=variants,
                    ),
                    incident_id=incident_id,
                    callback_base_url=settings.agent_callback_url,
                    webhook_secret=settings.webhook_secret,
                    approve_variants=approve_variants,
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
            # C-07: an ESCALATE whose only commands were RBAC-denied lands here (no approvable
            # command) — surface them as suggestions so the human still sees what to check.
            if denied_cmds:
                denied_str = "\n".join(f"- `{c}`" for c in denied_cmds)
                msg += (
                    "\n\n**Comandos sugeridos (el agente no tiene permisos para ejecutarlos):**\n"
                    + denied_str
                )
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
            # Camino único (F2): encolar en Redis Streams. Fail-closed — si Redis no
            # está o XADD falla, devolvemos 503 y Alertmanager reintenta (no perder alertas).
            if app.state.redis is None:
                logger.error("Cannot enqueue alert %s — Redis unavailable", alert_name)
                WEBHOOK_COUNTER.labels(status="error").inc()
                raise HTTPException(status_code=503, detail="alert queue unavailable")
            fingerprint = _alert_fingerprint(alert)
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
        else:
            # Resolved alerts: notify + R5 close-out (correlate by fingerprint back to
            # the firing incident → resolution metric + resolved_observed outcome).
            severity, _, pod, namespace = _extract_alert_meta(alert)
            msg = f"🟢 **[RESOLVED] [{severity}] {alert_name}** | Pod: `{pod}` | NS: `{namespace}`"
            background_tasks.add_task(send_mattermost_alert, msg)
            background_tasks.add_task(_correlate_resolution, _alert_fingerprint(alert))
        
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

    rollback_scheduled = False
    if action in ("approve", "approve_engine", "approve_model"):
        # C-08: run the command bound to the clicked button. A two-button escalation carries
        # {approve_engine, approve_model}; a single-button one carries {approve} (or is empty
        # for a pre-C-08 escalation → fall back to safe_commands). Same target either way —
        # only the value differs; the rollback safety net (below) is value-agnostic.
        commands_to_run = (
            [incident.command_variants[action]]
            if action in incident.command_variants else incident.safe_commands
        )
        # Human/auto parity (v2 Eje A): for a structured remediation the stored command IS
        # the engine synthesis, so the human path gets the same safety net as the auto path —
        # pre-patch snapshot (from the sealed target) + scheduled rollback evaluation.
        pre_patch_snapshot = None
        if is_structured_remediation(incident.diagnosis):
            try:
                pre_patch_snapshot = await capture_pre_patch_value(incident.diagnosis["proposed_action"])
            except Exception as exc:
                logger.warning("Pre-patch snapshot failed for %s: %s", incident.incident_id, exc)
        try:
            execute_results = await execute_commands(commands_to_run)
            log = results_to_log(execute_results)
            REMEDIATION_COUNTER.labels(action="human_approved").inc()
            remediation_for_feedback = {
                "action": RemediationAction.AUTO_REMEDIATE,
                "execution_log": log,
                "safe_commands": commands_to_run,
                "blocked_commands": [],
            }
            if (
                is_structured_remediation(incident.diagnosis)
                and any(r.success for r in execute_results)
                and redis_client is not None
            ):
                # C-02 (extiende F-01): seed the workload cooldown on human approve too.
                # The human is never gated by it (their judgment overrides), but the
                # window must exist so a second alert for the same root cause (OOM +
                # CrashLoop of the same pod) doesn't auto-patch on top of this patch
                # or raise a duplicate escalation. Fail-soft: the approve already ran.
                try:
                    pa = incident.diagnosis["proposed_action"]
                    await acquire_workload_cooldown(redis_client, pa["namespace"], pa["name"])
                except Exception as exc:
                    logger.warning(
                        "Cooldown seed failed after human approve",
                        extra={"incident_id": incident.incident_id, "error": str(exc)},
                    )
            if (
                settings.remediation_rollback_enabled
                and pre_patch_snapshot is not None
                and any(r.success for r in execute_results)
            ):
                # R2 human/auto parity: feed the verdict loop exactly like the auto path
                # (main.py auto branch) — pass the incident doc_id + remediation summary so
                # the rollback verdict re-upserts the SAME ChromaDB doc (cured/rolled_back)
                # and increments aiops_feedback_verdict_total. Without them ctx.doc_id="" and
                # _reupsert_incident_outcome short-circuits → a human-approved cure never
                # entered the learning loop.
                await _schedule_rollback_evaluation(
                    incident.incident_id, pre_patch_snapshot, incident.alert_item, incident.diagnosis,
                    doc_id=incident.incident_doc_id or "",
                    remediation=remediation_for_feedback,
                )
                rollback_scheduled = True
                # R5: the rollback verdict now owns this incident's outcome (the doc was
                # indexed as awaits_verdict=False at escalate/diagnosis time). Drop the
                # observational entry so a later `resolved` can't overwrite the
                # cured/rolled_back verdict with the weaker resolved_observed.
                await pop_active_incident(_alert_fingerprint(incident.alert_item), app.state.redis)
        except Exception as exc:
            logger.error("Command execution failed for incident %s: %s", incident.incident_id, exc)
            log = f"ERROR: {exc}"
            REMEDIATION_COUNTER.labels(action="human_approve_failed").inc()
            remediation_for_feedback = {
                "action": RemediationAction.AUTO_REMEDIATE,
                "execution_log": log,
                "safe_commands": commands_to_run,
                "blocked_commands": [],
            }
        decision_line = f"\n---\n✅ **Remediación APROBADA** por @{user}\n```\n{log}\n```"
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
        # R2 human/auto parity: reuse the original incident doc_id so this write updates
        # the SAME document the rollback verdict will later re-upsert (one doc per incident,
        # not a duplicate). When a rollback is pending, mark it auto_pending so retrieval
        # won't cite an unverified fix before the verdict flips it to cured/rolled_back.
        doc_id, text, metadata = build_incident_document(
            alert_data, incident.diagnosis, remediation_for_feedback,
            doc_id=incident.incident_doc_id or None,
            outcome=INCIDENT_OUTCOME_PENDING if rollback_scheduled else None,
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
    elif not settings.remediation_dry_run:
        # Fail-closed: with real execution enabled, an unconfigured command token
        # must reject rather than fail-open (symmetric to _verify_hmac_token). P0·2.
        raise HTTPException(status_code=401, detail="Command auth not configured")

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
        # _query_recent_incidents makes blocking ChromaDB calls; offload it so the
        # slash-command handler never blocks the event loop (F-03).
        rows = await asyncio.to_thread(_query_recent_incidents, app.state.chroma_client, limit)
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
