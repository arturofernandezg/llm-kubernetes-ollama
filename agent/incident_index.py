"""
Fingerprint → active-incident correlation index in Redis (R5 — observational loop).

When an incident is diagnosed and archived, its fingerprint is recorded here with
just enough context (doc_id, error_class, start time, and the already-built incident
document) to close the loop when Alertmanager later sends the matching `resolved`
alert: emit an MTTR-style resolution metric and, for incidents the auto rollback loop
does NOT own, re-upsert the ChromaDB doc as `resolved_observed`.

All functions are fail-open: they log a warning and return a safe default instead of
propagating, so a Redis outage degrades the learning loop but never breaks ingestion
or the resolved-alert webhook. TTL is enforced by Redis (SETEX) — an incident that
never resolves within the window simply expires and loses its resolution signal.

Key format: "incident:active:{fingerprint}"
"""

import json
import logging

logger = logging.getLogger("aiops_agent")

_PREFIX = "incident:active:"


def _key(fingerprint: str) -> str:
    return f"{_PREFIX}{fingerprint}"


async def record_active_incident(
    fingerprint: str, payload: dict, ttl_seconds: int, redis_client
) -> bool:
    """Record an active incident keyed by fingerprint with TTL. Returns True on success.

    A repeated firing of the same fingerprint overwrites the previous entry (latest
    incident wins) — consistent with the alert dedup window upstream.
    """
    if redis_client is None:
        return False
    try:
        await redis_client.setex(_key(fingerprint), ttl_seconds, json.dumps(payload))
        return True
    except Exception as exc:
        logger.warning("Redis record_active_incident failed for %s: %s", fingerprint, exc)
        return False


async def pop_active_incident(fingerprint: str, redis_client) -> dict | None:
    """Fetch and delete the active-incident entry for a fingerprint (atomic-ish:
    GET then DEL). Returns the payload dict, or None if absent / Redis down. Deletes
    so a resolution is consumed once and never double-counts."""
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_key(fingerprint))
        if raw is None:
            return None
        await redis_client.delete(_key(fingerprint))
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis pop_active_incident failed for %s: %s", fingerprint, exc)
        return None
