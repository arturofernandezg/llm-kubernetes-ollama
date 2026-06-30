---
fecha: 2026-06-29
slug: f2-retirar-legacy-ingesta
promoted: true
---

> Cuarta sesión del 2026-06-29 (tras `f2-slice4-validacion-cluster`). Arrancada con `/start`; plan en `~/.claude/plans/purrfect-frolicking-journal.md`. Validada la cola en cluster (Slice 4), retiramos el camino legacy de ingesta para dejar la cola como único path. Código yo, build/deploy/kubectl Jay.

## Objetivo
Retirar el camino legacy de ingesta (BackgroundTasks in-process + dedup `IN_FLIGHT_ALERTS`) ahora que la cola Redis Streams quedó validada en cluster. La cola pasa a ser el **camino único e incondicional**: el webhook siempre encola; si Redis cae → 503 fail-closed (Alertmanager reintenta). Eliminar el flag `queue_enabled` por completo (no dejarlo como kill-switch muerto).

## Hecho
- **`agent/config.py`**: borrado `queue_enabled: bool = False`; comentario del bloque cola actualizado (camino único, 503 fail-closed). Resto de settings `queue_*` intactos (los usa `streams.py`).
- **`agent/main.py`**:
  - Borrados `IN_FLIGHT_ALERTS` + `_INFLIGHT_LOCK` (estado global), la variable `dedup_key` en `_process_alert_with_diagnosis` y su `discard` en el `finally`.
  - Lifespan: el consumer + reclaim arrancan siempre que `app.state.redis is not None` (sin gate por flag); si Redis falta → `logger.error` (readyz dará 503 igual). Eliminada la rama `elif queue_enabled` ("falling back to background tasks").
  - `readyz`: chequea **siempre** Redis (quitada la rama legacy Ollama+modelo). Docstring simplificado.
  - Webhook firing: rama única → guard `redis is None` → 503; `enqueue_alert` con `try/except` → 503 si XADD falla; `entry_id is None` → `DEDUP_COUNTER` (dedup de la cola, NO legacy). `BackgroundTasks` se conserva solo para notificaciones de alertas `resolved`.
- **`agent/tests/test_endpoints.py`**:
  - Borradas `TestInFlightDedup` (4 tests) y `TestReadyzEndpoint` (3 tests de readyz legacy Ollama). Quitado import de `IN_FLIGHT_ALERTS`/`_INFLIGHT_LOCK` y el helper sin uso `mock_ollama_model_not_loaded`.
  - `TestWebhookQueuePath`: sin patches `queue_enabled`; añadido `test_redis_none_returns_503_fail_closed`; borrados `test_queue_disabled_uses_legacy_path` y `test_queue_enabled_but_redis_none_falls_back_to_legacy`.
  - `TestAlertmanagerWebhook` + `TestWebhookWithDiagnosis`: `app.state.redis = AsyncMock()` para que las firing den 200; `test_webhook_firing_enqueues_alert` (renombrado) asserta encolado, no llamada directa al pipeline. `test_health_follows_redirect_when_redis_ok` adaptado a `mode=queue`.
  - **Resultado: 112 passed** en `test_endpoints.py` (neto −8 tests vs antes). 2 warnings preexistentes de `escalation_store` (scan_iter sobre AsyncMock en TestSlashCommandEndpoint), ajenos.
- **`k8s/deployment-agent.yaml`**: borrado el env `QUEUE_ENABLED=true`; comentario del `readinessProbe` actualizado (chequea Redis, ya no condicional).
- **Build + deploy**: imagen nueva `aiops-agent:5d5d7c7` (tras commitear → rebuild → bump del tag). Aplicada en `arturo-llm-test`, rollout OK.
- **Smoke fail-closed en cluster** ✅: `kubectl scale deploy/redis --replicas=0` → `/readyz=503` (puerto 8000); reescalado a 1. Confirma que el código nuevo está vivo (la imagen vieja con cola off chequearía Ollama → 200).

## Encontrado / gotchas
- **Build antes de commit → tag de imagen equivocado (lección de orden).** Jay corrió `gcloud builds submit` con `COMMIT_SHA=$(git rev-parse --short HEAD)` ANTES de commitear → HEAD seguía en `6f11740`, la imagen salió tagueada `:6f11740` con el código nuevo dentro (gcloud tarea el working dir, no el commit). Con tags por sha-corto el orden obligatorio es **commit → build → bump del tag en manifiesto**. La imagen `:6f11740` quedó huérfana/mal-etiquetada (inocua).
- **El `kubectl apply` con el manifiesto apuntando a `:c0e6de6` redesplegó la imagen VIEJA + quitó el env QUEUE_ENABLED** → el cluster quedó momentáneamente corriendo legacy con la cola apagada (en `c0e6de6` el legacy aún existe, así que funcional pero regresivo). Causa: el manifiesto no se había bumpeado al tag nuevo. Detectado por mí leyendo `grep image: deployment-agent.yaml`.
- **El service `agent-svc` expone puerto 8000, no 80.** El primer smoke de readyz falló (`readyz=000`) porque el port-forward `18000:80` nunca se estableció (`error: Service agent-svc does not have a service port 80`). Con `18000:8000` → `readyz=503` correcto. Patrón de PF confirmado: `pkill -f "port-forward svc/agent-svc"; sleep 2; kubectl port-forward svc/agent-svc 18000:8000 ... & sleep 3; curl`.
- **`enqueue_alert` con `AsyncMock` de Redis devuelve truthy → 200 sin patchear.** `redis.set(nx)` y `redis.xadd` sobre AsyncMock retornan mocks truthy → `is_new` truthy, `entry_id` no-None. Por eso basta `app.state.redis = AsyncMock()` en los tests de webhook firing para que den 200, sin mockear `enqueue_alert`.

## Decisiones + por qué
- **Eliminar el flag `queue_enabled` por completo (no mantenerlo default True).** Sin camino legacy, la rama `queue_enabled=False` quedaría sin comportamiento (config muerta / wart). Un flag muerto es peor que ningún flag. La bitácora previa decía "default True" pero eso era antes de razonar que el flag se vuelve vestigial. (Jay eligió esta opción explícitamente sobre "mantener flag default True" y sobre "Redis-down = degradar síncrono").
- **Redis caído = 503 fail-closed, sin degradación síncrona.** Reintroducir procesamiento in-process al caer Redis sería reintroducir parte del legacy y contradice "no perder alertas" (Alertmanager reintenta el 503). Coherente con readyz gated por Redis: el pod sale de rotación, no procesa fuera de la cola.
- **`BackgroundTasks` se conserva** solo para la notificación simple de alertas `resolved` (no requieren cola ni diagnóstico). No es legacy.
- **`DEDUP_COUNTER` se conserva**: la dedup ahora vive en la cola (`enqueue_alert` → `entry_id is None`), el counter lo sigue alimentando ese camino.
- **Commits separados (código vs manifiesto+tag)** para que el tag del manifiesto referencie la imagen ya construida del commit de código, evitando la circularidad sha↔manifiesto.

## Siguiente
- **`/promote` de F2 completa** (Slices 1-4 + retiro de legacy, ya en estado limpio):
  - `docs/07`: cerrar F2 (cola = único camino), changelog, y el modo de fallo "Agente reinicio mid-diagnóstico → se pierde la alerta" pasa a **resuelto por la cola**.
  - `docs/02`: endpoints/métricas `aiops_queue_*`, readyz gated por Redis (ya no condicional), webhook fail-closed.
  - `docs/14`: production-readiness (cola valida durabilidad; PR-02/03/07 cerrados conceptualmente).
  - `CLAUDE.md`: `streams.py`, imagen `5d5d7c7`, nuevo conteo de tests, quitar mención a `IN_FLIGHT_ALERTS`/dedup in-flight del Estado actual.
  - Marcar las **4 bitácoras del 2026-06-29** como `promoted: true`.
- **Confirmar conteo total de la suite** (`cd agent && python -m pytest -q`) para fijar el número exacto en docs.
- **Paso F (opcional)**: paneles Grafana `aiops_queue_*` (enqueued vs processed, depth, reclaimed, dead) — encaja con Gate 8.
- Pendiente de siempre (cluster): matriz E1–E6 (`docs/14`).
