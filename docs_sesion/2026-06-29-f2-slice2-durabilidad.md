---
fecha: 2026-06-29
slug: f2-slice2-durabilidad
promoted: true
---

> Sesión arrancada con `/start`; Slice 1 verificado por Jay (126 tests del subset streams+endpoints en verde). Micro-objetivo: **Slice 2 de F2** — durabilidad de la cola (reclaim + dead-letter), todo detrás de `QUEUE_ENABLED=False`, testeable con `AsyncMock`. Plan aprobado en `~/.claude/plans/sequential-dazzling-stonebraker.md`.

## Objetivo
Cerrar el hueco de durabilidad que Slice 1 dejó abierto: hoy una entrada cuyo handler falla o cuyo pod muere mid-procesamiento queda **pendiente en el PEL del consumer group y nadie la vuelve a tocar** (`consume_loop` solo lee `>`, nunca el PEL). Añadir reclaim de pendientes + dead-letter por delivery-count + métricas de durabilidad. Sin tocar el pipeline ni los tests existentes.

## Hecho
- **`agent/config.py`** — 3 settings nuevos: `queue_reclaim_interval_seconds=60`, `queue_min_idle_seconds=600`, `queue_dead_letter_key="aiops:alerts:dead"` (`queue_max_deliveries=3` ya existía de Slice 1).
- **`agent/streams.py`**:
  - Métricas nuevas: `QUEUE_RECLAIMED` / `QUEUE_DEAD` (Counters sin labels) + `QUEUE_DEPTH` (Gauge). Import de `Gauge` añadido.
  - Helper `_ack_and_process(redis_client, handler, entry_id, fields)` extraído de `consume_loop` → handler + XACK en éxito; si lanza, no XACK y propaga. Reutilizado por `consume_loop` (refactorizado) y `reclaim_pending`.
  - `reclaim_pending(redis_client, handler) -> tuple[int,int]`: `xpending_range` idle-filtrado (`idle=min_idle_ms`) → poison (`times_delivered > max_deliveries`) a dead-letter (`xclaim` para recuperar payload + `xadd` al DLQ con `orig_id`/`deliveries` forenses + `xack` original) ; resto `xclaim` + `_ack_and_process` (XCLAIM sube el delivery-count → poison eventual). Actualiza `QUEUE_DEPTH` con `xpending` summary `["pending"]`. **Fail-soft** (a diferencia del fail-closed de `enqueue_alert`).
- **`agent/main.py`**:
  - `reclaim_pending` añadido al `from streams import (...)`.
  - `_periodic_reclaim()` (patrón `_periodic_cleanup`): 1ª iteración inmediata (recuperación de arranque) → `sleep(queue_reclaim_interval_seconds)`. Fail-soft por iteración (`try/except` envolviendo `reclaim_pending`, el error no mata la tarea).
  - `lifespan`: `app.state.reclaim_task` creado junto a `consumer_task` dentro del bloque `if queue_enabled and redis`; inicializado a `None`; cancelación en shutdown unificada en un `for _task in (consumer_task, reclaim_task)`.
- **Tests**: +5 en `test_streams.py` (`TestReclaimPending`: noop / reprocesa+ack / dead-letter poison / handler-fail-sin-ack / depth gauge) + 2 en `test_endpoints.py` (`TestPeriodicReclaim`: delega en reclaim_pending con el handler correcto / error no mata la tarea). Añadido `import asyncio` a nivel de módulo en `test_endpoints.py`.
- Verificado `python3 -m py_compile` sobre los 5 ficheros. **Tests pendientes de correr por Jay.**

## Encontrado / gotchas
- **`min_idle_time` debe superar el tiempo de diagnóstico (~205-252s).** Durante un diagnóstico normal la entrada está delivered-sin-ACK todo ese rato; si el reclaim usara un idle pequeño robaría trabajo en curso del propio `consume_loop` (o de un pod hermano con réplicas>1) → doble procesamiento. Por eso `queue_min_idle_seconds=600` (10 min, margen). Mismo umbral en arranque y periódico → durabilidad con retraso acotado (≤600s), coherente con el at-least-once ya asumido.
- **XAUTOCLAIM no devuelve el delivery-count**, que es justo lo que decide el dead-letter → se usa `XPENDING` (trae `times_delivered`) + `XCLAIM` explícito, en vez de XAUTOCLAIM directo.
- **`consume_loop` solo lee `>`**, nunca el PEL → el reclaim **debe reprocesar in-place** (handler + XACK dentro de `reclaim_pending`); de ahí el helper compartido para no duplicar la lógica.
- **`import asyncio` solo estaba local** (dentro de funciones) en `test_endpoints.py`; los tests nuevos lo usan a nivel de método → `py_compile` no lo detecta (no resuelve nombres) pero fallaría en runtime. Subido a import de módulo.
- Las métricas sin labels (`QUEUE_RECLAIMED`/`QUEUE_DEAD`) y el Gauge emiten a 0 desde el arranque sin pre-init → no se tocó el bloque de pre-init de `lifespan`.

## Decisiones + por qué
- **Reclaim fail-soft (no fail-closed como `enqueue_alert`):** un fallo de Redis en el reclaim se loguea y se reintenta al próximo tick; la durabilidad ya la garantiza la persistencia del PEL, no hace falta propagar/503. El fail-closed solo tiene sentido en el productor (no perder la alerta entrante).
- **Dead-letter a un stream de cuarentena (`aiops:alerts:dead`) con campos forenses**, no descarte silencioso: un poison message (>3 entregas) no debe bloquear la cola para siempre, pero se conserva para inspección/demo ("mira, aquí van las alertas envenenadas").
- **Tarea periódica separada (`_periodic_reclaim`)** en vez de colgar del `_periodic_cleanup` existente: concern separado, gated por flag, intervalo propio. Cancelación unificada con `consumer_task` en el shutdown (mismo ciclo de vida).
- **Helper `_ack_and_process` compartido** en vez de duplicar handler→XACK en `consume_loop` y `reclaim_pending`: una sola fuente de verdad para la semántica "procesa y confirma; si falla, deja pendiente".

## Siguiente
- **Que Jay corra los tests**: `pytest agent/tests/test_streams.py agent/tests/test_endpoints.py -q` y luego `pytest agent/tests -q` (suite completa en verde, legacy intacto).
- **Slice 3 — PR-02 + limpieza**: `/readyz` gated por Redis (quitar Ollama del readiness — el sentido de la cola es bufferear durante lentitud de Ollama), retirar `IN_FLIGHT_ALERTS` in-memory (el dedup ya vive en Redis), subir Redis 64→128Mi (`k8s/redis.yaml`).
- **Slice 4 (cluster)**: desplegar `QUEUE_ENABLED=true`, burst test, matar pod mid-burst → demostrar replay (`aiops_queue_reclaimed_total` sube), forzar 3 fallos → `aiops_queue_dead_total` + entrada en `aiops:alerts:dead`, métricas a Grafana; luego cola default + retirar legacy.
- Pendiente de siempre (cluster): hornear imagen (PR-01/04/05/06 + F2 Slices 1-2 solo en código) · matriz E1–E6 (`docs/14`) · Gate 8 (screenshots Grafana).
- **`/promote`** de la cola completa (07/02/14/CLAUDE.md + vault) al **cerrar F2**, no por slice.
