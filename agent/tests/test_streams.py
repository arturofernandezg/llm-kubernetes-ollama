"""
Tests para agent/streams.py — cola Redis Streams (F2, Slice 1).

FakeRedis NO soporta operaciones de stream (XADD/XREADGROUP/XGROUP/SET NX),
así que se mockea Redis con AsyncMock (patrón del diseño F2).

Comportamiento bajo test:
- enqueue_alert: encola OK / dedup skip / fail-closed (propaga si Redis cae)
  / compensación de la dedup-key si el XADD falla (F-05)
- ensure_group: BUSYGROUP idempotente / otros errores se propagan
- consume_loop: procesa+XACK / handler que lanza no hace XACK y sigue
- consumer_name: lee HOSTNAME
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ResponseError

import streams
from streams import (
    enqueue_alert, ensure_group, consume_loop, consumer_name, reclaim_pending,
)


# ── consumer_name ───────────────────────────────────────────────────────────

class TestConsumerName:
    def test_reads_hostname(self):
        with patch.dict("os.environ", {"HOSTNAME": "agent-7c9-abc"}):
            assert consumer_name() == "agent-7c9-abc"

    def test_default_when_no_hostname(self):
        with patch.dict("os.environ", {}, clear=True):
            assert consumer_name() == "agent-local"


# ── enqueue_alert ───────────────────────────────────────────────────────────

class TestEnqueueAlert:
    @pytest.mark.asyncio
    async def test_enqueues_when_new(self):
        r = AsyncMock()
        r.set.return_value = True          # SET NX → clave nueva
        r.xadd.return_value = "1700000000-0"

        entry_id = await enqueue_alert(r, '{"payload": "x"}', "OOM:prod:pod-1")

        assert entry_id == "1700000000-0"
        r.set.assert_awaited_once()
        # SET ... NX EX <window>
        assert r.set.call_args.kwargs.get("nx") is True
        assert r.set.call_args.kwargs.get("ex") == streams.settings.dedup_window_seconds
        # XADD ... MAXLEN ~ <maxlen>
        r.xadd.assert_awaited_once()
        assert r.xadd.call_args.kwargs.get("maxlen") == streams.settings.queue_maxlen
        assert r.xadd.call_args.kwargs.get("approximate") is True

    @pytest.mark.asyncio
    async def test_dedup_skips_xadd(self):
        r = AsyncMock()
        r.set.return_value = None          # SET NX → clave ya existía

        entry_id = await enqueue_alert(r, '{"payload": "x"}', "OOM:prod:pod-1")

        assert entry_id is None
        r.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_fail_closed_propagates_on_redis_down(self):
        r = AsyncMock()
        r.set.side_effect = Exception("Connection refused")

        with pytest.raises(Exception, match="Connection refused"):
            await enqueue_alert(r, '{"payload": "x"}', "OOM:prod:pod-1")

    @pytest.mark.asyncio
    async def test_xadd_failure_deletes_dedup_key_and_propagates(self):
        # F-05: sin la compensación, la dedup-key huérfana convertiría el retry
        # de Alertmanager en un 200 "duplicado benigno" sin encolar nada.
        r = AsyncMock()
        r.set.return_value = True
        r.xadd.side_effect = Exception("XADD failed")

        with pytest.raises(Exception, match="XADD failed"):
            await enqueue_alert(r, '{"payload": "x"}', "OOM:prod:pod-1")

        r.delete.assert_awaited_once_with("aiops:seen:OOM:prod:pod-1")

    @pytest.mark.asyncio
    async def test_compensation_delete_failure_warns_and_propagates_xadd_error(self, caplog):
        r = AsyncMock()
        r.set.return_value = True
        r.xadd.side_effect = Exception("XADD failed")
        r.delete.side_effect = Exception("delete failed too")

        with pytest.raises(Exception, match="XADD failed"):
            await enqueue_alert(r, '{"payload": "x"}', "OOM:prod:pod-1")

        assert any(
            "compensation failed" in rec.message for rec in caplog.records
        )


# ── ensure_group ────────────────────────────────────────────────────────────

class TestEnsureGroup:
    @pytest.mark.asyncio
    async def test_creates_group_with_mkstream(self):
        r = AsyncMock()
        await ensure_group(r)
        r.xgroup_create.assert_awaited_once()
        assert r.xgroup_create.call_args.kwargs.get("mkstream") is True
        # Default de arranque: desde el inicio del stream.
        assert r.xgroup_create.call_args.kwargs["id"] == "0"

    @pytest.mark.asyncio
    async def test_busygroup_is_idempotent(self):
        r = AsyncMock()
        r.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        # No debe lanzar
        await ensure_group(r)

    @pytest.mark.asyncio
    async def test_other_error_propagates(self):
        r = AsyncMock()
        r.xgroup_create.side_effect = Exception("WRONGTYPE")
        with pytest.raises(Exception, match="WRONGTYPE"):
            await ensure_group(r)


# ── consume_loop ────────────────────────────────────────────────────────────

def _one_entry_then_cancel(entry_id="1-0", payload='{"payload": "x"}'):
    """xreadgroup: primera llamada devuelve 1 entrada, segunda corta el loop."""
    resp = [[streams.settings.queue_stream_key, [(entry_id, {"payload": payload})]]]
    return [resp, asyncio.CancelledError()]


class TestConsumeLoop:
    @pytest.mark.asyncio
    async def test_processes_entry_and_acks(self):
        r = AsyncMock()
        r.xreadgroup.side_effect = _one_entry_then_cancel()
        handler = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consume_loop(r, handler)

        handler.assert_awaited_once_with("1-0", {"payload": '{"payload": "x"}'})
        r.xack.assert_awaited_once()
        ack_args = r.xack.call_args.args
        assert ack_args[0] == streams.settings.queue_stream_key
        assert ack_args[1] == streams.settings.queue_group
        assert ack_args[2] == "1-0"

    @pytest.mark.asyncio
    async def test_handler_failure_skips_ack_and_continues(self):
        r = AsyncMock()
        r.xreadgroup.side_effect = _one_entry_then_cancel()
        handler = AsyncMock(side_effect=Exception("pipeline boom"))

        with pytest.raises(asyncio.CancelledError):
            await consume_loop(r, handler)

        handler.assert_awaited_once()
        r.xack.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_response_keeps_blocking(self):
        r = AsyncMock()
        # BLOCK expira (None/[]) y luego cortamos
        r.xreadgroup.side_effect = [[], asyncio.CancelledError()]
        handler = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consume_loop(r, handler)

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_nogroup_recreates_group_and_backs_off(self):
        # El grupo desapareció (Redis recreado bajo un agente vivo) → self-heal:
        # ensure_group regenera el grupo y hay backoff (no busy-spin).
        r = AsyncMock()
        r.xreadgroup.side_effect = [
            ResponseError("NOGROUP No such key 'aiops:alerts' or consumer group"),
            asyncio.CancelledError(),
        ]
        handler = AsyncMock()

        with patch("streams.asyncio.sleep", AsyncMock()) as sleep_mock:
            with pytest.raises(asyncio.CancelledError):
                await consume_loop(r, handler)

        # ensure_group → xgroup_create (MKSTREAM) para regenerar el grupo.
        r.xgroup_create.assert_awaited_once()
        # start_id="$": solo entregas futuras → no re-entrega el historial retenido.
        assert r.xgroup_create.call_args.kwargs["id"] == "$"
        # Backoff aplicado en vez de continue inmediato (prueba de no-busy-spin).
        sleep_mock.assert_awaited_once()
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_xreadgroup_error_backs_off_without_recreate(self):
        # Error transitorio de Redis (no NOGROUP) → backoff, sin tocar el grupo.
        r = AsyncMock()
        r.xreadgroup.side_effect = [
            Exception("Error 104 connection reset by peer"),
            asyncio.CancelledError(),
        ]
        handler = AsyncMock()

        with patch("streams.asyncio.sleep", AsyncMock()) as sleep_mock:
            with pytest.raises(asyncio.CancelledError):
                await consume_loop(r, handler)

        r.xgroup_create.assert_not_called()
        sleep_mock.assert_awaited_once()
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_counter_resets_after_success(self):
        # Error → éxito (entrada procesada+XACK) → cancel. El contador se resetea
        # tras el éxito; el camino normal sigue funcionando.
        resp = [[streams.settings.queue_stream_key, [("1-0", {"payload": "x"})]]]
        r = AsyncMock()
        r.xreadgroup.side_effect = [
            Exception("transient"),
            resp,
            asyncio.CancelledError(),
        ]
        handler = AsyncMock()

        with patch("streams.asyncio.sleep", AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await consume_loop(r, handler)

        handler.assert_awaited_once_with("1-0", {"payload": "x"})
        r.xack.assert_awaited_once()


# ── reclaim_pending ─────────────────────────────────────────────────────────

def _pending(entry_id="1-0", deliveries=1):
    """Item de xpending_range (formato redis-py)."""
    return {
        "message_id": entry_id,
        "consumer": "agent-dead",
        "time_since_delivered": 700_000,
        "times_delivered": deliveries,
    }


class TestReclaimPending:
    @pytest.mark.asyncio
    async def test_no_pending_is_noop(self):
        r = AsyncMock()
        r.xpending_range.return_value = []
        r.xpending.return_value = {"pending": 0}
        handler = AsyncMock()

        reclaimed, dead = await reclaim_pending(r, handler)

        assert (reclaimed, dead) == (0, 0)
        handler.assert_not_called()
        r.xclaim.assert_not_called()

    @pytest.mark.asyncio
    async def test_reclaims_and_reprocesses(self):
        r = AsyncMock()
        r.xpending_range.return_value = [_pending("1-0", deliveries=1)]
        r.xclaim.return_value = [("1-0", {"payload": '{"payload": "x"}'})]
        r.xpending.return_value = {"pending": 1}
        handler = AsyncMock()

        before = streams.QUEUE_RECLAIMED._value.get()
        reclaimed, dead = await reclaim_pending(r, handler)

        assert (reclaimed, dead) == (1, 0)
        handler.assert_awaited_once_with("1-0", {"payload": '{"payload": "x"}'})
        r.xack.assert_awaited_once()
        assert streams.QUEUE_RECLAIMED._value.get() - before == 1

    @pytest.mark.asyncio
    async def test_dead_letters_poison_entry(self):
        r = AsyncMock()
        # times_delivered > queue_max_deliveries (3) → poison
        r.xpending_range.return_value = [_pending("9-0", deliveries=4)]
        r.xclaim.return_value = [("9-0", {"payload": '{"payload": "bad"}'})]
        r.xpending.return_value = {"pending": 1}
        handler = AsyncMock()

        before = streams.QUEUE_DEAD._value.get()
        reclaimed, dead = await reclaim_pending(r, handler)

        assert (reclaimed, dead) == (0, 1)
        handler.assert_not_called()
        # XADD al stream de cuarentena con campos forenses
        r.xadd.assert_awaited_once()
        dlq_args = r.xadd.call_args.args
        assert dlq_args[0] == streams.settings.queue_dead_letter_key
        assert dlq_args[1]["orig_id"] == "9-0"
        assert dlq_args[1]["deliveries"] == "4"
        r.xack.assert_awaited_once()
        assert streams.QUEUE_DEAD._value.get() - before == 1

    @pytest.mark.asyncio
    async def test_handler_failure_skips_ack(self):
        r = AsyncMock()
        r.xpending_range.return_value = [_pending("1-0", deliveries=2)]
        r.xclaim.return_value = [("1-0", {"payload": '{"payload": "x"}'})]
        r.xpending.return_value = {"pending": 1}
        handler = AsyncMock(side_effect=Exception("pipeline boom"))

        reclaimed, dead = await reclaim_pending(r, handler)

        assert (reclaimed, dead) == (0, 0)
        handler.assert_awaited_once()
        r.xack.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_depth_gauge(self):
        r = AsyncMock()
        r.xpending_range.return_value = []
        r.xpending.return_value = {"pending": 7}
        handler = AsyncMock()

        await reclaim_pending(r, handler)

        assert streams.QUEUE_DEPTH._value.get() == 7
