"""
Tests for agent/escalation_store.py — Redis-backed persistent escalation storage.

All tests use FakeRedis (in-memory stub) — no real Redis required.
Behavior under test: store/get/delete/count + fail-open when Redis raises.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from escalation_store import (
    store_escalation,
    get_escalation,
    delete_escalation,
    count_escalations,
)
from tests.helpers import FakeRedis


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sample_payload(incident_id: str = "test-001") -> dict:
    return {
        "incident_id": incident_id,
        "alert_item": {
            "status": "firing",
            "labels": {"alertname": "OOMKilled", "pod": "p", "namespace": "n", "severity": "critical"},
            "annotations": {"description": "OOM"},
            "startsAt": "2026-01-01T00:00:00Z",
        },
        "diagnosis": {"diagnosis": "OOM", "confidence": 0.9, "risk": "high", "commands": []},
        "safe_commands": ["kubectl describe pod p -n n"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
    }


# ── store_escalation ──────────────────────────────────────────────────────────

class TestStoreEscalation:
    @pytest.mark.asyncio
    async def test_store_writes_json_to_redis(self):
        r = FakeRedis()
        payload = _sample_payload()
        ok = await store_escalation("test-001", payload, ttl_seconds=3600, redis_client=r)
        assert ok is True
        raw = r._store.get("escalation:test-001")
        assert raw is not None
        loaded = json.loads(raw)
        assert loaded["incident_id"] == "test-001"

    @pytest.mark.asyncio
    async def test_store_returns_false_on_exception(self):
        broken = AsyncMock()
        broken.setex = AsyncMock(side_effect=ConnectionError("redis down"))
        ok = await store_escalation("x", {}, ttl_seconds=60, redis_client=broken)
        assert ok is False

    @pytest.mark.asyncio
    async def test_store_key_prefix_is_escalation(self):
        r = FakeRedis()
        await store_escalation("abc", {"k": "v"}, ttl_seconds=60, redis_client=r)
        assert "escalation:abc" in r._store


# ── get_escalation ────────────────────────────────────────────────────────────

class TestGetEscalation:
    @pytest.mark.asyncio
    async def test_get_returns_stored_payload(self):
        r = FakeRedis()
        payload = _sample_payload()
        await store_escalation("test-001", payload, ttl_seconds=3600, redis_client=r)
        result = await get_escalation("test-001", redis_client=r)
        assert result is not None
        assert result["incident_id"] == "test-001"

    @pytest.mark.asyncio
    async def test_get_returns_none_when_key_missing(self):
        r = FakeRedis()
        result = await get_escalation("nonexistent", redis_client=r)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_on_redis_error(self):
        broken = AsyncMock()
        broken.get = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await get_escalation("test-001", redis_client=broken)
        assert result is None


# ── delete_escalation ─────────────────────────────────────────────────────────

class TestDeleteEscalation:
    @pytest.mark.asyncio
    async def test_delete_removes_key(self):
        r = FakeRedis()
        payload = _sample_payload()
        await store_escalation("del-001", payload, ttl_seconds=60, redis_client=r)
        await delete_escalation("del-001", redis_client=r)
        assert r._store.get("escalation:del-001") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_is_noop(self):
        r = FakeRedis()
        # Should not raise
        await delete_escalation("nonexistent", redis_client=r)

    @pytest.mark.asyncio
    async def test_delete_fails_open_on_redis_error(self):
        broken = AsyncMock()
        broken.delete = AsyncMock(side_effect=ConnectionError("redis down"))
        # Should not raise
        await delete_escalation("any", redis_client=broken)


# ── count_escalations ─────────────────────────────────────────────────────────

class TestCountEscalations:
    @pytest.mark.asyncio
    async def test_count_zero_when_empty(self):
        r = FakeRedis()
        assert await count_escalations(r) == 0

    @pytest.mark.asyncio
    async def test_count_returns_number_of_stored_escalations(self):
        r = FakeRedis()
        await store_escalation("c-001", _sample_payload("c-001"), ttl_seconds=60, redis_client=r)
        await store_escalation("c-002", _sample_payload("c-002"), ttl_seconds=60, redis_client=r)
        assert await count_escalations(r) == 2

    @pytest.mark.asyncio
    async def test_count_only_counts_escalation_prefix(self):
        r = FakeRedis()
        r.set_raw("other:key", "something")
        await store_escalation("c-003", _sample_payload("c-003"), ttl_seconds=60, redis_client=r)
        assert await count_escalations(r) == 1

    @pytest.mark.asyncio
    async def test_count_returns_zero_on_redis_error(self):
        broken = AsyncMock()

        async def broken_scan(*args, **kwargs):
            raise ConnectionError("redis down")
            yield  # make it an async generator

        broken.scan_iter = broken_scan
        assert await count_escalations(broken) == 0

    @pytest.mark.asyncio
    async def test_count_reflects_deletion(self):
        r = FakeRedis()
        await store_escalation("d-001", _sample_payload("d-001"), ttl_seconds=60, redis_client=r)
        await store_escalation("d-002", _sample_payload("d-002"), ttl_seconds=60, redis_client=r)
        await delete_escalation("d-001", redis_client=r)
        assert await count_escalations(r) == 1


# ── End-to-end flow ───────────────────────────────────────────────────────────

class TestEscalationStoreFlow:
    @pytest.mark.asyncio
    async def test_store_get_delete_flow(self):
        """Full store → get → delete → get=None flow."""
        r = FakeRedis()
        payload = _sample_payload("flow-001")
        await store_escalation("flow-001", payload, ttl_seconds=3600, redis_client=r)
        retrieved = await get_escalation("flow-001", redis_client=r)
        assert retrieved is not None
        assert retrieved["incident_id"] == "flow-001"
        await delete_escalation("flow-001", redis_client=r)
        gone = await get_escalation("flow-001", redis_client=r)
        assert gone is None
