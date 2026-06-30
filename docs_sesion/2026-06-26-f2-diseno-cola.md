---
fecha: 2026-06-26
slug: f2-diseno-cola
promoted: true
---

> Sesión arrancada con `/start`; micro-objetivo elegido por Jay: **diseño** de F2 (cola Redis Streams), sin código. Diseñado con ultrathink y plan mode. Deliverable = plan aprobado (`~/.claude/plans/splendid-waddling-whale.md`).

## Objetivo
Diseñar F2 — cola Redis Streams entre el webhook y el pipeline — para desacoplar la ingesta del LLM lento, sobrevivir a reinicios mid-burst (PR-07) y dar dedup cluster-wide (PR-03). Solo diseño, no código.

## Hecho
- **Trazado del pipeline real** sobre `main.py`: hoy `/webhook/alert` hace dedup in-memory (`IN_FLIGHT_ALERTS`) y lanza `_process_alert_with_diagnosis` como `BackgroundTasks` en el mismo proceso → no durable.
- **Plan de diseño aprobado** con 4 slices (3 de código testeable con `AsyncMock` + 1 de cluster). Ficheros futuros: nuevo `agent/streams.py`; editar `main.py`/`config.py`/`k8s/redis.yaml`; tests `test_streams.py`.
- **3 decisiones arquitectónicas fijadas con Jay** (ver abajo).
- Verificado infra de tests: se mockea Redis con `AsyncMock` sobre funciones que reciben `redis_client` (patrón `escalation_store.py`); **no hay `fakeredis`** → el diseño lo respeta (funciones puras + `_handle_stream_entry` aislado).

## Encontrado / gotchas
- **Insight que orienta todo el diseño:** Ollama **serializa** la generación (un modelo, una instancia). Por tanto multiplicar workers NO sube throughput — solo encolaría en Ollama. El valor de la cola es **durabilidad + absorber ráfagas + dedup**, no paralelismo. → consumidor procesa 1 alerta a la vez, in-process (sin Deployment aparte).
- **At-least-once es inevitable** con reclaim (XAUTOCLAIM): un crash tras LLM antes de XACK reprocesa → posible Mattermost duplicado / remediación repetida. Mitigación: dedup-key por fingerprint suaviza duplicados + dead-letter por delivery-count (`MAX_DELIVERIES=3`). Semántica honesta a documentar.
- **Redis tiene límite 64Mi** (manifiesto) y el stream lo compartiría con las `escalation:` keys → subir a 128Mi + `XADD MAXLEN ~ 1000`.
- **`app.state.redis` ya existe** (aioredis con ping en lifespan, `main.py:328-338`) → productor y consumidor lo reutilizan, no se crea otro cliente.
- `_process_alert_with_diagnosis` **no se toca**: la cola solo cambia *quién lo dispara*. `_handle_stream_entry` lo invoca igual.
- Evitar nombre `queue.py` (colisión con stdlib en imports planos del paquete) → `streams.py`.

## Decisiones + por qué
- **Topología in-process (no worker aparte):** un solo Deployment/imagen/RBAC/secrets. Como Ollama serializa, un Deployment de worker no compra throughput y duplica infra. Con réplicas>1, el consumer group ya da dedup+durabilidad gratis (cada entry → un solo consumidor). Se reconsidera worker aparte solo si Ollama escala.
- **PR-02 — readiness gated por Redis, desacoplado de Ollama:** el sentido de la cola es bufferear durante lentitud/reinicio de Ollama; por eso Ollama-down ya NO debe marcar unready (las alertas bufferean, el worker drena al volver). `/readyz` pasa a comprobar Redis. El webhook hace **fail-closed** (503 si XADD falla) — excepción consciente al fail-open del resto del sistema, porque el objetivo es no perder alertas (Alertmanager reintenta). Aislado en Slice 3 para no mezclar con el plumbing.
- **Rollout tras flag `QUEUE_ENABLED=false` (default):** los 394 tests pasan sin tocar; el camino de cola convive con BackgroundTasks tras el flag. Se valida en cluster (Slice 4) y luego se hace default y se retira el legacy. Deuda temporal asumida, reversible — "despacio y bien".
- **Dedup migra a Redis (SETNX por fingerprint `alertname:ns:pod` con TTL):** sustituye `IN_FLIGHT_ALERTS` in-memory por un mecanismo compartido → cluster-wide (PR-03), suprime reenvíos de Alertmanager y suaviza duplicados de replay. Reusa `aiops_dedup_skipped_total`.

## Siguiente
- **Slice 1 — camino de cola E2E tras flag:** `agent/streams.py` (`enqueue_alert` con dedup SETNX + `XADD MAXLEN ~`, `ensure_group` con MKSTREAM/ignorar BUSYGROUP, `consume_loop`, `consumer_name()`) · productor en `/webhook/alert` con 503 fail-closed · `consume_loop` arrancado en `lifespan` · `_handle_stream_entry` en `main.py` delegando en `_process_alert_with_diagnosis` · settings en `config.py` (`queue_enabled`, `queue_stream_key`, `queue_group`, `dedup_window_seconds`, `queue_max_deliveries`, `queue_maxlen`) · métricas `aiops_queue_*` · tests `test_streams.py` con `AsyncMock`.
- **Slice 2:** XAUTOCLAIM reclaim (arranque + periódico) + dead-letter por delivery-count.
- **Slice 3:** `/readyz` Redis-gated (PR-02) + retirar `IN_FLIGHT_ALERTS` + Redis 64→128Mi.
- **Slice 4 (cluster):** desplegar `QUEUE_ENABLED=true`, burst test, matar pod mid-burst → demostrar replay, métricas a Grafana; luego cola default + retirar legacy.
- Plan completo en `~/.claude/plans/splendid-waddling-whale.md`.
- Pendiente de siempre (cluster): hornear imagen (PR-01/04/05/06 aún solo en código) · matriz E1–E6 (`docs/14`) · Gate 8 (screenshots Grafana).
