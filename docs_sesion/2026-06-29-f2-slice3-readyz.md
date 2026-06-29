---
fecha: 2026-06-29
slug: f2-slice3-readyz
promoted: false
---

> Segunda sesión del 2026-06-29 (tras `f2-slice2-durabilidad`, ya verde: 120 tests subset + 427 suite completa). Arrancada con `/start`; plan aprobado en `~/.claude/plans/smooth-sauteeing-hopper.md`. Micro-objetivo: **Slice 3 de F2 (parcial)** — readyz condicional + bump Redis. `IN_FLIGHT_ALERTS` se difirió a Slice 4 por decisión de planificación.

## Objetivo
Cerrar la parte de Slice 3 que no requiere cluster: que `/readyz` deje de sacar al pod de rotación cuando Ollama está lento/caído **en modo cola** (el sentido de la cola es bufferear esa lentitud), y dar holgura de memoria a Redis ahora que aloja cola + PEL + dead-letter (no solo escalaciones).

## Hecho
- **`agent/main.py`** (`/readyz`, ~L475): endpoint condicional por `settings.queue_enabled`:
  - Cola activa → chequea **solo Redis**: `getattr(app.state, "redis", None)` (`None` → 503 `"Redis unavailable (queue mode)"`), si no `await asyncio.wait_for(redis.ping(), timeout=settings.health_timeout)` (excepción → 503 `"Redis unreachable: ..."`), OK → `{"status":"ready","mode":"queue","redis":"up"}`. **No** consulta Ollama.
  - Legacy (`queue_enabled=False`, default) → chequeo Ollama+modelo EXACTAMENTE como antes (sin tocar esa rama).
  - Reusa `settings.health_timeout` (5.0s, ya existía) y `asyncio` (ya importado).
- **`k8s/redis.yaml`** — `requests.memory` 32→64Mi, `limits.memory` 64→128Mi. CPU sin tocar.
- **`agent/tests/test_endpoints.py`** — nuevo `TestReadyzQueueMode` (×3): Redis OK → 200 `mode=queue` + verifica que `ping` se await-eó y Ollama (mock unreachable) se ignora / `redis=None` → 503 / `ping` lanza → 503. `teardown_method` resetea `app.state.redis=None`. Los `test_readyz_*` legacy intactos (cubren la rama por defecto).
- Verificado `python3 -m py_compile main.py tests/test_endpoints.py`.
- **Tests corridos por Jay**: `pytest tests/test_endpoints.py -q` → **120 passed**; `pytest tests -q` → **427 passed**, 5 warnings (ruido pre-existente de AsyncMock coroutine-never-awaited, no fallos).

## Encontrado / gotchas
- **El swap de readyz tenía que ser condicional, no Redis-siempre.** `queue_enabled` sigue en `False` por defecto y el deploy actual es legacy → un readyz Redis-only cambiaría el comportamiento del pod desplegado HOY (quedaría Ready con Ollama caído en un modo que aún procesa inline). Gated por flag = cero regresión en lo desplegado.
- **Patrón de test de modo cola ya existía** (`TestSlashCommand`/queue tests): `app.state.redis = AsyncMock()` + `patch.object(main.settings, "queue_enabled", True)`. Reutilizado en vez de inventar fixture. `AsyncMock().ping` es awaitable por defecto y `assert_awaited_once()` confirma que se consultó.
- **Conteo de tests 394 → 427** acumulado en las dos sesiones de hoy (Slice 1+2+3). El doc canónico dice 394 — pendiente de actualizar en `/promote`.
- Warning `escalation_store.py:58` (scan_iter AsyncMock never awaited) sigue ahí; pre-existe a esta sesión, no introducido aquí.

## Decisiones + por qué
- **readyz condicional por `queue_enabled`** (vs Redis-siempre / vs híbrido Ollama+Redis): elegido condicional para no regresionar el modo legacy aún desplegado. Cuando la cola esté activa, el ingress solo necesita poder **encolar** (Redis); procesar (Ollama) es problema del worker. Cuando esté en legacy, sigue necesitando Ollama porque procesa inline. La condición desaparecerá sola cuando se retire el legacy (Slice 4).
- **Diferir `IN_FLIGHT_ALERTS` a Slice 4** (estaba en el "Siguiente" de Slice 2 como parte de Slice 3): hoy vive **solo** en el camino legacy (`main.py:918-927`); el camino de cola ya dedup-ea por fingerprint en Redis (`enqueue_alert`). Quitarlo ahora dejaría una imagen legacy **sin dedup** → Alertmanager reenviando durante un diagnóstico de ~205s dispararía pipelines duplicados. Se retira junto con el camino legacy (Slice 4), nunca antes, para que ninguna imagen quede dedup-less.
- **Bump Redis 64→128Mi ahora** (no esperar a Slice 4): es cambio de manifiesto, no de imagen; se aplica al desplegar la cola. `queue_maxlen=1000` + PEL + stream de dead-letter necesitan más que el uso actual (solo estado de escalaciones, kilobytes).

## Siguiente
- **Slice 4 (cluster, requiere hornear imagen):**
  - Hornear imagen nueva con TODO lo acumulado en código: PR-01/04/05/06 (F1 quick-wins) + F2 Slices 1-2-3.
  - Aplicar `k8s/redis.yaml` (128Mi) y desplegar con `QUEUE_ENABLED=true`.
  - Burst test: ráfaga de N alertas sin perder ninguna; matar el pod del agente mid-burst → demostrar replay (`aiops_queue_reclaimed_total` sube); forzar 3 fallos de un mismo mensaje → `aiops_queue_dead_total` + entrada en `aiops:alerts:dead`. Métricas de cola a Grafana.
  - Validar readyz en cluster: parar/matar Redis → `/readyz` da 503; Ollama lento → `/readyz` sigue 200 (pod en rotación, la cola absorbe).
  - **Retirar `IN_FLIGHT_ALERTS` + `TestInFlightDedup` + camino legacy** del webhook, y hacer `QUEUE_ENABLED=true` el default en `config.py`.
- Pendiente de siempre (cluster): matriz E1–E6 (`docs/14`) · Gate 8 (screenshots Grafana).
- **`/promote`** de F2 completa (07/02/14/CLAUDE.md — incl. conteo de tests 427 — + vault) al **cerrar F2**, no por slice.
