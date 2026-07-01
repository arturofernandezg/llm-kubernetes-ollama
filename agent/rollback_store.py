"""
Persistent store for in-flight rollback contexts using Redis.

Durability backstop for the in-memory IN_FLIGHT_ROLLBACKS dict (P0·3, auditoría
2026-07-01). If the pod restarts during the rollback wait window, the in-memory
context and its sleeping task are lost and the patch would NEVER be reverted.
Persisting the context lets a fresh pod recover at startup and still evaluate
(and revert) the patch.

All functions are fail-open: they log a warning and return a safe default
(False / None / []) instead of propagating, mirroring escalation_store.

Key format: "rollback:{incident_id}". TTL is enforced by Redis.
"""

import json
import logging

logger = logging.getLogger("aiops_agent")

_PREFIX = "rollback:"


def _key(incident_id: str) -> str:
    return f"{_PREFIX}{incident_id}"


async def store_rollback(incident_id: str, payload: dict, ttl_seconds: int, redis_client) -> bool:
    """Persist a serialized rollback context with TTL. Returns True on success."""
    try:
        await redis_client.setex(_key(incident_id), ttl_seconds, json.dumps(payload))
        return True
    except Exception as exc:
        logger.warning("Redis store_rollback failed for %s: %s", incident_id, exc)
        return False


async def delete_rollback(incident_id: str, redis_client) -> None:
    """Delete a rollback context from Redis. Fail-open."""
    try:
        await redis_client.delete(_key(incident_id))
    except Exception as exc:
        logger.warning("Redis delete_rollback failed for %s: %s", incident_id, exc)


async def list_rollbacks(redis_client) -> list[dict]:
    """Return all persisted rollback payloads (for startup recovery). Empty list on error."""
    try:
        out: list[dict] = []
        async for k in redis_client.scan_iter(match=f"{_PREFIX}*"):
            raw = await redis_client.get(k)
            if raw is not None:
                out.append(json.loads(raw))
        return out
    except Exception as exc:
        logger.warning("Redis list_rollbacks failed: %s", exc)
        return []
