"""
Persistent store for pending human escalations using Redis.

All functions are fail-open: they log a warning and return a safe default
(None / 0 / False) instead of propagating exceptions. This lets the agent
degrade gracefully when Redis is unavailable.

Key format: "escalation:{incident_id}"
TTL is enforced by Redis — no manual expiry checks needed at the app layer.
"""

import json
import logging

logger = logging.getLogger("aiops_agent")

_PREFIX = "escalation:"


def _key(incident_id: str) -> str:
    return f"{_PREFIX}{incident_id}"


async def store_escalation(incident_id: str, payload: dict, ttl_seconds: int, redis_client) -> bool:
    """Persist serialized escalation dict with TTL. Returns True on success."""
    try:
        await redis_client.setex(_key(incident_id), ttl_seconds, json.dumps(payload))
        return True
    except Exception as exc:
        logger.warning("Redis store_escalation failed for %s: %s", incident_id, exc)
        return False


async def get_escalation(incident_id: str, redis_client) -> dict | None:
    """Retrieve a serialized escalation dict. Returns None if not found or Redis down."""
    try:
        raw = await redis_client.get(_key(incident_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis get_escalation failed for %s: %s", incident_id, exc)
        return None


async def delete_escalation(incident_id: str, redis_client) -> None:
    """Delete escalation from Redis. Fail-open."""
    try:
        await redis_client.delete(_key(incident_id))
    except Exception as exc:
        logger.warning("Redis delete_escalation failed for %s: %s", incident_id, exc)


async def count_escalations(redis_client) -> int:
    """Count pending escalations via SCAN. Returns 0 on error."""
    try:
        n = 0
        async for _ in redis_client.scan_iter(match=f"{_PREFIX}*"):
            n += 1
        return n
    except Exception as exc:
        logger.warning("Redis count_escalations failed: %s", exc)
        return 0
