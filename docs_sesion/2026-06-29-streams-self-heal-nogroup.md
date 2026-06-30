---
fecha: 2026-06-29
slug: streams-self-heal-nogroup
promoted: true
---

> Sexta sesión del 2026-06-29. Arrancada con `/start`. Cierra el ★ PRIORITARIO que dejó la sesión `grafana-cola-tenancy-observabilidad`: el fix de código de la causa raíz del HighCPU de Redis (busy-spin NOGROUP), no el parche de subir el límite de CPU. Código y tests yo; pytest/commit Jay.

## Objetivo
- **Fix raíz en `consume_loop` (`agent/streams.py`)**: hacer el consumidor self-healing ante `XREADGROUP failed: NOGROUP` (el grupo desaparece cuando Redis se recrea/flushea bajo un agente vivo, sin PVC). Hoy el `except` hace `continue` inmediato → busy-spin de cientos de iter/s que satura la CPU de Redis. Es lo que disparaba HighCPU, no la carga de la cola.

## Hecho
- **`agent/streams.py` — `consume_loop` self-healing**:
  - `import asyncio` + `from utils import backoff_delay` añadidos.
  - El `except` del `xreadgroup` ya **no** hace `continue` inmediato. Contador local `consecutive_failures` (incrementa en `except`, **resetea a 0** tras un `xreadgroup` con éxito) → `await asyncio.sleep(backoff_delay(consecutive_failures - 1, retry_base_delay, retry_max_delay))`. Backoff exponencial acotado (mismo helper/patrón que `main.py:1141`).
  - Si el error contiene `"NOGROUP"` → `await ensure_group(redis_client, start_id="$")` antes del backoff (regenera el grupo). Otro error → solo log + backoff.
  - `CancelledError` (no es `Exception` en py3.11) sigue propagando → parada limpia intacta.
  - Docstring de `consume_loop` actualizado (self-heal + por qué del `$`).
- **`agent/streams.py` — `ensure_group(redis_client, start_id="0")`**: nuevo parámetro. `id=start_id` en el `xgroup_create`. Default `"0"` preserva el arranque (lifespan). El self-heal lo invoca con `"$"`. Docstring explica los dos modos.
- **`agent/tests/test_streams.py`** (+3 tests en `TestConsumeLoop`, import `ResponseError` de `redis.exceptions`):
  - `test_nogroup_recreates_group_and_backs_off`: NOGROUP → `xgroup_create` awaited **con `id="$"`** + `streams.asyncio.sleep` awaited (prueba de no-busy-spin); handler no llamado.
  - `test_generic_xreadgroup_error_backs_off_without_recreate`: error genérico → `xgroup_create` NO llamado, `sleep` SÍ.
  - `test_failure_counter_resets_after_success`: error → éxito (procesa+XACK) → cancel; el camino normal sigue tras el reset del contador.
  - Reforzados los existentes: self-heal sella `id="$"`; `TestEnsureGroup.test_creates_group_with_mkstream` sella el default `id="0"`.
- **Suite**: Jay corrió `pytest -q` → **422 passed**, 5 warnings (preexistentes y ajenos: escalation_store, remediation, rollback — no de este cambio). 419→422.
- `py_compile` de ambos ficheros OK en local (`agent/.venv/bin/python`).

## Encontrado / gotchas
- **El residuo de replay (catch de Jay, evitó un bug).** Tras el primer fix (recrear el grupo con el `ensure_group` original = `id="0"`), Jay preguntó "¿no dejará residuos que se siguen enviando?". Análisis: hay **dos sub-casos** de NOGROUP:
  1. **Redis recreado entero** (el caso REAL de la sesión anterior: bump de recursos sin PVC → pod nuevo) → el stream se va con él → MKSTREAM crea stream **vacío** → `id=0` inocuo, cero replay. El fix v1 ya era correcto aquí.
  2. **El grupo se borra pero el stream sobrevive** (p.ej. `XGROUP DESTROY`, o el propio escenario de prueba que yo había sugerido en el plan) → MKSTREAM ve el stream con su historia → recrear en `id=0` **re-entrega TODO el historial retenido** (hasta `MAXLEN ~1000`). Y crucialmente ese replay **NO pasa por `enqueue_alert`** → la dedup-key `aiops:seen:<fp>` **no lo frena** → 1000 re-diagnósticos LLM + spam Mattermost + remediaciones sobre estados ya resueltos.
- **`backoff_delay` es 0-indexed** (`base * 2**attempt`): primer fallo → `consecutive_failures=1` → paso `consecutive_failures - 1 = 0` → delay = base = 1.0s. Correcto.
- **El default `id="0"` de `ensure_group` no es replay-peligroso en el arranque** porque la primerísima vez el stream está vacío, y en restarts posteriores el grupo ya existe (BUSYGROUP → el id se ignora; lo pendiente lo recupera `reclaim_pending` vía PEL). El riesgo de `id=0` solo aparece al **recrear** un grupo sobre un stream con historia → exclusivo del self-heal.

## Decisiones + por qué
- **Self-heal recrea el grupo con `id="$"`, no `id="0"`.** Trade-off: `$` se salta las entradas que entraron durante el gap NOGROUP (posible pérdida temporal), pero evita el replay masivo permanente del historial retenido. Se elige `$` porque (a) el daño del replay es grande y no recuperable (1000 diagnósticos sobre estados viejos), y (b) la pérdida del gap **sí** es recuperable: Alertmanager reenvía las alertas firing periódicamente (`repeat_interval`). Pérdida temporal recuperable < replay masivo permanente.
- **Parámetro `start_id` con default `"0"` en `ensure_group` (no función nueva ni hardcode en dos sitios).** Mantiene `ensure_group` como único punto de creación del grupo, idempotente, reutilizado por lifespan y self-heal; el comportamiento de arranque queda intacto por el default.
- **Backoff con el helper compartido `backoff_delay` + settings de retry existentes** (`retry_base_delay`/`retry_max_delay`), no constantes nuevas. Coherente con `main.py`/`mattermost.py`; el spin se convierte en reintentos espaciados sin introducir config nueva.
- **El bump de Redis 50m→150m (sesión anterior) se queda como defensa en profundidad**, no se revierte. Ahora la raíz está curada en código; el headroom solo cubre picos legítimos de la cola.
- **No se tocó `reclaim_pending`**: su `continue`/fail-soft es por tick (cada `queue_reclaim_interval_seconds`), no un loop apretado → no hay busy-spin que arreglar ahí.

## Siguiente
- **Commit** (Jay, sin Co-Authored-By): `fix: consume_loop self-healing ante NOGROUP con id=$ (mata busy-spin sin replay)` → `agent/streams.py` + `agent/tests/test_streams.py`.
- **Actualizar conteo de tests 419→422** en `docs/07-roadmap.md` y `CLAUDE.md` (regla "docs reflect reality"; suite ya confirmada en verde por Jay) — se hará en `/promote`.
- **`/promote`** cuando se consolide: este fix es el "fix real de F2-durabilidad" → modos de fallo en `docs/07` (fila "Agente reinicio mid-diagnóstico" / "Cola") + nota en `docs/02` sobre el self-heal de `consume_loop` + CLAUDE.md (descripción de `streams.py`: añadir self-heal NOGROUP con `id=$`). Material de production-readiness para `docs/14` ("qué pasa con la durabilidad cuando Redis se recrea bajo un agente vivo": busy-spin curado, gap cubierto por Alertmanager, replay evitado por `$`).
- **Validación en cluster (Jay, cuando aplique)**: con la cola drenada, `redis-cli XGROUP DESTROY aiops:alerts aiops-workers` **sin** reiniciar el agente → logs deben mostrar `NOGROUP → recreating` + `Consumer group created`, `rate(cpu redis[5m])` NO se dispara (sin busy-spin), HighCPU deja de firing, y NO hay replay de alertas viejas en Mattermost. Requiere imagen nueva (build + bump del Deployment del agente).
- Pendiente de siempre (cluster): aplicar bump Redis + corte de ingestión de la sesión anterior (ver `2026-06-29-grafana-cola-tenancy-observabilidad.md` §Siguiente); matriz E1–E6 (`docs/14`); F3 (HPA/CPU); screenshot Gate 8 con caudal.
