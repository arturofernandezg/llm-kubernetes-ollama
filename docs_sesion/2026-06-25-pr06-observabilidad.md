---
fecha: 2026-06-25
slug: pr06-observabilidad
promoted: true
---

> Segunda sesión del día (la primera, `production-readiness-f1`, cerró PR-04). Arranque con `/start`; micro-objetivo elegido por Jay: **PR-06**.

## Objetivo
Cerrar el quick-win de código **PR-06** de F1 (huecos de observabilidad), sin tocar cluster. Elegido sobre PR-05 por valor de demo: que Grafana distinga tipos de fallo en vez de ser caja negra para la presentación al chapter.

## Hecho
- **`agent/main.py` — (a) split de outcome del LLM**: `aiops_diagnosis_total` ya distinguía las dos ramas internamente (`httpx.TimeoutException` vs `Exception`) pero las etiquetaba igual (`llm_failed`). Ahora emite `outcome="llm_timeout"` (rama timeout) y `outcome="llm_error"` (resto). Comentario de labels del counter (L61) actualizado. Cero cambio de lógica.
- **`agent/main.py` — (b) counter de salud de escalación**: nuevo `ESCALATION_STORE_COUNTER = Counter("aiops_escalation_store_total", ..., ["outcome"])` tras `DEDUP_COUNTER`. En el bloque de escalación: `stored=True` → `outcome="stored"` (rama con botones); `else` (Redis `None` **o** `store_escalation`→False) → `outcome="redis_down"` (rama degradada sin botones).
- **`agent/tests/test_endpoints.py`**: 2 tests extendidos (`test_timeout_exception_sets_llm_timeout_flag` asierta delta en `{outcome="llm_timeout"}`; `test_non_timeout_exception_uses_generic_fallback` en `{outcome="llm_error"}`, vía helper `_get_counter`). Clase nueva `TestEscalationStoreMetric` con 2 tests (stored incrementa + llama `send_escalation_with_buttons`; redis_down con `redis_client=None` incrementa + mensaje "Redis caído" sin botones).
- **Docs actualizadas en la misma sesión** (entregable F1, no esperan a `/promote`): `docs/11` PR-06 → DONE; `docs/14` §2 PR-06 marcado ✅ con el fix, §3 tabla → hecho, matriz E2/E2b/E3 con las nuevas señales de métrica.
- Pendiente: que Jay corra `cd agent && source .venv/bin/activate && pytest tests/test_endpoints.py -q` (no ejecutado aún al cerrar el log).

## Encontrado / gotchas
- **Las dos ramas timeout/error ya existían** en el código (FASE 2 ya había metido `_llm_timeout` para diferenciar el mensaje a Mattermost) — PR-06(a) era puramente exponer en la métrica una distinción que el código ya hacía. Coste casi nulo, valor de demo alto.
- **Ningún test existente asertaba sobre `outcome="llm_failed"`** (grep confirmado) → el renombrado de labels no rompe nada. Riesgo cero.
- **`_get_counter(name, labels)`** (helper en `test_endpoints.py:1184`, sobre `REGISTRY.get_sample_value`) se define *después* de `TestDiagnosisTimeout`, pero resuelve en runtime como global → se puede usar en clases anteriores sin problema.
- Los formatters `_format_escalation_header`/`_format_escalation_body` corren *antes* de `send_escalation_with_buttons` (patcheado en el test) y usan `.get()` defensivo sobre el diagnosis dict → compatibles con `mock_diagnosis_result()`, no hace falta patchearlos.

## Decisiones + por qué
- **Counter `aiops_escalation_store_total`, no gauge `aiops_redis_up`** (el finding ofrecía ambos). Razones: (1) el módulo solo importa `Counter, Histogram` → coherencia de patrón, sin import nuevo; (2) un counter no queda *stale* como un gauge sin refresco periódico; (3) registra el **evento de negocio real** (escalación que no se pudo persistir), que es justo la señal que falta en E3 e incrementa de forma visible durante el chaos de Redis. Para la narrativa de demo, "escalaciones degradadas por Redis caído" cuenta mejor que un 1/0.
- **Métrica en el punto de escalación, no en startup ni en un health-check periódico**: captura el fallo *cuando importa* (hay una escalación que se degrada). Un ping de startup o un gauge global serían más ruido para el alcance de un quick-win de ~20 min.
- **"0 pending" engañoso de `/aiops` dejado fuera de alcance**: es un cambio en `_format_status_response` (mostrar "Redis DOWN" en vez de 0), ortogonal a la métrica de PR-06. Documentado explícitamente como fuera de alcance en `docs/11` y `docs/14` para no fingir que se resolvió.
- **Actualizar `docs/11` y `docs/14` ahora (no en `/promote`)**: `docs/14` es el entregable de F1 y se mantiene vivo en sesión; marcar PR-06 done ahí es reflejar realidad, no promover hechos dispersos. La promoción a 07/CLAUDE.md/vault sigue siendo trabajo de `/promote`.

## Siguiente
- **Que Jay corra la suite** (`tests/test_endpoints.py`, idealmente la completa) y confirme verde antes de promover. 2 tests extendidos + 2 nuevos.
- **Próximo quick-win de F1**: queda **PR-05** (reconexión lazy de `chroma_client`: si es `None` o falla `retrieve_context`, intentar `get_chroma_client()` en caliente) y **PR-01** (alinear default `http_timeout` de `config.py` a 300 para reproducibilidad local). Ambos esfuerzo S.
- **Cuando Jay tenga sesión `kubectl`**: ejecutar la matriz E1–E6 de `docs/14`, rellenar veredictos; ahora E2/E2b/E3 tienen señales de métrica concretas que observar (`llm_error`/`llm_timeout`/`escalation_store{redis_down}`).
- Pendiente real de siempre: **Gate 8** — screenshots Grafana.
- Cuando se cierren varios quick-wins: `/promote` para consolidar PR-04 + PR-06 (y lo que caiga) a docs canónicos + vault.
