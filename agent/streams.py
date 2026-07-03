"""
Cola Redis Streams entre el webhook y el pipeline de diagnóstico (Fase 2).

Motivación: hoy `/webhook/alert` dispara el pipeline (RAG → LLM → remediación)
como BackgroundTask en el mismo proceso → no durable (si el pod reinicia
mid-diagnóstico la alerta se pierde) y el dedup es per-pod. Con una cola durable
el webhook encola en O(1) (XADD) y un consumidor in-process drena de a una.

Insight de diseño: Ollama serializa la generación (un modelo, una instancia),
así que la cola NO compra paralelismo — su valor es durabilidad + absorber
ráfagas + dedup cluster-wide.

Patrón (como escalation_store.py): funciones puras que reciben `redis_client`,
testeables con AsyncMock. Excepción consciente: `enqueue_alert` es **fail-closed**
(propaga si Redis cae) para que el webhook devuelva 503 y Alertmanager reintente
— no perder alertas prima sobre el fail-open del resto del sistema.

Convención de claves:
  - stream:    settings.queue_stream_key   (p.ej. "aiops:alerts")
  - dedup:     "aiops:seen:<fingerprint>"  con TTL settings.dedup_window_seconds
"""

import asyncio
import logging
import os

from prometheus_client import Counter, Gauge

from config import settings
from utils import backoff_delay

logger = logging.getLogger("aiops_agent")

_SEEN_PREFIX = "aiops:seen:"

QUEUE_ENQUEUED = Counter(
    "aiops_queue_enqueued_total",
    "Alerts enqueued to the Redis stream",
)
QUEUE_PROCESSED = Counter(
    "aiops_queue_processed_total",
    "Alerts drained from the stream by outcome",
    ["outcome"],  # "success" | "error"
)
QUEUE_RECLAIMED = Counter(
    "aiops_queue_reclaimed_total",
    "Pending entries reclaimed and reprocessed",
)
QUEUE_DEAD = Counter(
    "aiops_queue_dead_total",
    "Poison entries dead-lettered after exceeding max deliveries",
)
QUEUE_DEPTH = Gauge(
    "aiops_queue_depth",
    "Pending (delivered, unacked) entries in the consumer group",
)


def consumer_name() -> str:
    """Nombre del consumidor para el consumer group (HOSTNAME del pod → trazable en XAUTOCLAIM)."""
    return os.environ.get("HOSTNAME", "agent-local")


def _seen_key(fingerprint: str) -> str:
    return f"{_SEEN_PREFIX}{fingerprint}"


async def ensure_group(redis_client, start_id: str = "0") -> None:
    """
    Crea el consumer group (con MKSTREAM) de forma idempotente.

    `XGROUP CREATE` lanza si el grupo ya existe (error BUSYGROUP); lo ignoramos.
    Cualquier otro error se propaga (problema real de Redis al arrancar).

    `start_id` fija desde dónde entrega el grupo:
      - "0" (arranque): desde el inicio del stream. La primera vez el stream está
        vacío → inocuo; tras un restart del pod con Redis intacto el grupo ya
        existe (BUSYGROUP) y el id se ignora (lo pendiente lo recupera el PEL).
      - "$" (self-heal en caliente, ver `consume_loop`): solo entregas futuras.
        Evita que, si el grupo se recrea sobre un stream que SOBREVIVIÓ (p.ej.
        XGROUP DESTROY), se re-entregue todo el historial retenido (MAXLEN ~) →
        replay masivo de alertas viejas (no pasa por la dedup-key de enqueue).
    """
    try:
        await redis_client.xgroup_create(
            name=settings.queue_stream_key,
            groupname=settings.queue_group,
            id=start_id,
            mkstream=True,
        )
        logger.info(
            "Consumer group created",
            extra={"stream": settings.queue_stream_key, "group": settings.queue_group},
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            logger.info(
                "Consumer group already exists",
                extra={"stream": settings.queue_stream_key, "group": settings.queue_group},
            )
            return
        raise


async def enqueue_alert(redis_client, alert_json: str, fingerprint: str) -> str | None:
    """
    Encola una alerta firing. **Fail-closed**: propaga si Redis falla.

    1. Dedup cluster-wide: `SET aiops:seen:<fp> 1 NX EX <window>`. Si la clave ya
       existe (otro pod / reenvío de Alertmanager dentro de la ventana) → None.
    2. `XADD <stream> MAXLEN ~ <maxlen> * {"payload": alert_json}` → devuelve el id.

    Si el XADD falla tras marcar la dedup-key, la clave se borra antes de
    propagar (compensación): sin ella, el retry de Alertmanager chocaría con la
    dedup-key huérfana → 200 "duplicado benigno" sin encolar nada → la alerta
    se perdería de verdad hasta el repeat_interval.

    Devuelve el id del stream, o None si fue deduplicado. Lanza si Redis cae.
    """
    is_new = await redis_client.set(
        _seen_key(fingerprint), "1", nx=True, ex=settings.dedup_window_seconds
    )
    if not is_new:
        return None

    try:
        entry_id = await redis_client.xadd(
            settings.queue_stream_key,
            {"payload": alert_json},
            maxlen=settings.queue_maxlen,
            approximate=True,
        )
    except Exception:
        try:
            await redis_client.delete(_seen_key(fingerprint))
        except Exception:
            logger.warning(
                "Dedup-key compensation failed after XADD error; alert %s "
                "may be suppressed until the dedup window expires",
                fingerprint,
            )
        raise
    QUEUE_ENQUEUED.inc()
    logger.info(
        "Alert enqueued",
        extra={"stream": settings.queue_stream_key, "entry_id": entry_id, "fingerprint": fingerprint},
    )
    return entry_id


async def _ack_and_process(redis_client, handler, entry_id, fields) -> None:
    """
    Procesa una entrada y la confirma (XACK) en éxito. Compartido por el camino
    normal (`consume_loop`) y el de recuperación (`reclaim_pending`).

    Si el handler lanza, **no** se hace XACK: la entrada queda pendiente en el PEL
    para que la reclame un ciclo posterior. Propaga la excepción al llamante.
    """
    await handler(entry_id, fields)
    await redis_client.xack(
        settings.queue_stream_key, settings.queue_group, entry_id
    )


async def consume_loop(redis_client, handler) -> None:
    """
    Bucle consumidor in-process: drena el stream de a una entrada.

    `XREADGROUP ... COUNT 1 BLOCK 5000 STREAMS <stream> >` → por cada entrada,
    invoca `await handler(entry_id, fields)` y, en éxito, `XACK`. Si el handler
    lanza, NO se hace XACK (la entrada queda pendiente; la reclama el Slice 2 vía
    XAUTOCLAIM) y el bucle continúa. `CancelledError` se propaga (parada limpia).

    Self-heal ante fallo de XREADGROUP: si el error es **NOGROUP** (el grupo
    desapareció — Redis recreado/flusheado bajo un agente vivo, sin PVC) se
    regenera el grupo con `ensure_group(start_id="$")` (solo entregas futuras:
    si el stream sobrevivió, NO re-entrega el historial retenido → evita un
    replay masivo; el gap lo cubre el reenvío periódico de Alertmanager).
    Cualquier otro error es transitorio (Redis caído). En ambos casos se hace
    **backoff exponencial** antes de reintentar — nunca `continue` inmediato —
    para no entrar en un busy-spin que satura la CPU de Redis (HighCPU firing).

    Procesamiento secuencial (1 in-flight): rate-limit natural de Ollama.
    """
    consumer = consumer_name()
    logger.info(
        "Stream consumer loop started",
        extra={"stream": settings.queue_stream_key, "group": settings.queue_group, "consumer": consumer},
    )
    consecutive_failures = 0
    while True:
        try:
            resp = await redis_client.xreadgroup(
                groupname=settings.queue_group,
                consumername=consumer,
                streams={settings.queue_stream_key: ">"},
                count=1,
                block=5000,
            )
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            if "NOGROUP" in str(exc):
                logger.warning("Consumer group missing (NOGROUP), recreating: %r", exc)
                try:
                    await ensure_group(redis_client, start_id="$")
                except Exception as ge:
                    logger.error("ensure_group during self-heal failed: %r", ge)
            else:
                logger.warning("XREADGROUP failed, backing off: %r", exc)
            await asyncio.sleep(
                backoff_delay(
                    consecutive_failures - 1,
                    settings.retry_base_delay,
                    settings.retry_max_delay,
                )
            )
            continue

        if not resp:
            continue  # BLOCK expiró sin entradas

        for _stream_key, entries in resp:
            for entry_id, fields in entries:
                try:
                    await _ack_and_process(redis_client, handler, entry_id, fields)
                except Exception as exc:
                    # No XACK: la entrada queda pendiente para reclaim (reclaim_pending).
                    logger.error("Stream handler failed for %s: %r", entry_id, exc)


async def reclaim_pending(redis_client, handler) -> tuple[int, int]:
    """
    Recupera entradas pendientes (delivered pero sin XACK) y reprocesa o descarta.

    `consume_loop` solo lee entradas nuevas (`>`), nunca el PEL. Sin esta función,
    una entrada cuyo handler falló o cuyo pod murió mid-procesamiento quedaría
    pendiente para siempre → aquí está la durabilidad real de la cola.

    Flujo (XPENDING + XCLAIM, no XAUTOCLAIM: este último no devuelve el
    delivery-count, que es justo lo que decide el dead-letter):

      1. `XPENDING` range filtrado por idle (`> queue_min_idle_seconds`). El umbral
         supera el peor caso de diagnóstico (~252s) para no robar trabajo en curso
         del consumer activo (o de un pod hermano con réplicas>1).
      2. `times_delivered > queue_max_deliveries` → **dead-letter**: copia el payload
         al stream de cuarentena (`queue_dead_letter_key`) con campos forenses,
         XACK del original, descarta. Evita que un poison message bloquee la cola.
      3. resto → `XCLAIM` (sube el delivery-count) + reprocesa vía `_ack_and_process`.
         Si el handler vuelve a fallar, no se hace XACK: el próximo ciclo sube el
         count hasta que cruce el umbral y acabe en dead-letter.

    Fail-soft (a diferencia del fail-closed de `enqueue_alert`): un fallo de Redis
    se loguea y se reintenta al próximo tick; la durabilidad la garantiza el PEL.

    Devuelve (reclaimed, dead).
    """
    consumer = consumer_name()
    min_idle_ms = settings.queue_min_idle_seconds * 1000
    reclaimed = 0
    dead = 0

    pending = await redis_client.xpending_range(
        name=settings.queue_stream_key,
        groupname=settings.queue_group,
        min="-",
        max="+",
        count=settings.queue_maxlen,
        idle=min_idle_ms,
    )

    for item in pending:
        entry_id = item["message_id"]
        deliveries = item["times_delivered"]

        if deliveries > settings.queue_max_deliveries:
            # Dead-letter: recupera el payload para forensics y descarta.
            claimed = await redis_client.xclaim(
                settings.queue_stream_key, settings.queue_group, consumer,
                min_idle_ms, [entry_id],
            )
            fields = claimed[0][1] if claimed else {}
            await redis_client.xadd(
                settings.queue_dead_letter_key,
                {**fields, "orig_id": entry_id, "deliveries": str(deliveries)},
            )
            await redis_client.xack(
                settings.queue_stream_key, settings.queue_group, entry_id
            )
            QUEUE_DEAD.inc()
            dead += 1
            logger.error(
                "Entry dead-lettered after %d deliveries",
                deliveries,
                extra={"entry_id": entry_id, "dead_letter": settings.queue_dead_letter_key},
            )
            continue

        # Reclaim y reprocesa (XCLAIM sube el delivery-count para futuros ciclos).
        claimed = await redis_client.xclaim(
            settings.queue_stream_key, settings.queue_group, consumer,
            min_idle_ms, [entry_id],
        )
        if not claimed:
            continue  # otro consumidor lo reclamó antes (carrera benigna)
        _eid, fields = claimed[0]
        try:
            await _ack_and_process(redis_client, handler, _eid, fields)
            QUEUE_RECLAIMED.inc()
            reclaimed += 1
            logger.info("Pending entry reclaimed and reprocessed", extra={"entry_id": _eid})
        except Exception as exc:
            # No XACK: queda pendiente; el delivery-count ya subió → acabará dead-lettered.
            logger.error("Reclaim handler failed for %s: %r", _eid, exc)

    # Backlog real = entradas entregadas sin ACK (summary de XPENDING).
    summary = await redis_client.xpending(
        settings.queue_stream_key, settings.queue_group
    )
    QUEUE_DEPTH.set(summary["pending"] if summary else 0)

    return reclaimed, dead
