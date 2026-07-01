---
fecha: 2026-07-01
slug: v2-kickoff-eje-b-fail-closed
promoted: true
---

## Objetivo
Arrancar la **v2** del sistema tras la auditoría de hoy. Sesión doble:
(1) fijar la **tesis v2** y su secuencia (brainstorm sobre el roadmap activo);
(2) ejecutar la primera microtarea — **Eje B / P0·2: cerrar el fail-open de auth**
(HMAC callbacks + token slash + alinear `secrets-setup.sh`).

## Tesis v2 (brainstorm — durable, para /promote)
Todo el proyecto ha seguido un mismo vector: **quitarle autoridad al LLM y dársela a
fuentes deterministas**.
```
v0  el modelo decide Y ejecuta
v1  el modelo propone, el MOTOR dispone  (re-sourcing: comando sintetizado)   ← estábamos aquí
v2  el CLUSTER informa, el modelo razona, el motor dispone                    ← el salto
```
El re-sourcing (F3) selló el *comando*, pero el motor lo sella con
`name/ns/container/current` que **salen del LLM** → el 1.5b los alucina. El eslabón que
falta no es más inteligencia, es **grounding**: investigar como un SRE (logs/events/
`get -o json`) **antes** de razonar. Evidencia propia: "contexto > tamaño de modelo" (ya
medido; qwen3.5 no aporta y da timeout).

**Tres ejes v2 y su orden:**
- **Eje A — Grounding (keystone):** `agent/enrichment.py`. Gather K8s paralelo (get/logs/
  events) fail-soft, inyectado en `generate_diagnosis`; `current_value`/target desde el
  snapshot, no del LLM. Absorbe la slice C de F3 y el P0·1. Sesión grande (2-3 microtasks;
  se construye con kubectl mockeado sin cluster). Colapsa además el bug "approve humano
  ejecuta free-text del LLM" (humano y auto comparten el comando determinista).
- **Eje B — Confianza (credibilidad prod):** P0·2 (fail-closed auth) + P0·3 (rollback
  durable en Redis). Baratos, sin cluster para el código, altísimo ratio.
- **Eje C — Cobertura (historia de valor):** detectar Pending/Evicted/NotReady/FailedJob
  vía KSM (cero código, solo reglas de alerta). Amplía el "manejamos incidentes reales".

F4 (aprendizaje RAG) y F5 (predicción) son **v2.5/v3** — no compiten por la ventana de julio.
**Narrativa chapter:** "v1 detecta y remedia; me auté-audité como revisor senior, encontré
3 razones para rechazarlo en prod, v2 las cierra" → proyecta madurez de ingeniería.

## Plan de HOY — Eje B / P0·2 (fail-closed de auth)
**Diagnóstico (auditoría):** dos agujeros que se agravan mutuamente.
1. **Código fail-open incondicional.**
   - `_verify_hmac_token` (main.py:150): `if not webhook_secret: return True` → callback de
     botón sin autenticar. Con `DRY_RUN=false`, un "approve" no autenticado → remediación real.
   - Slash `/webhook/command` (main.py:1055): solo valida token si `mm_command_token` set.
     (Nota: `/aiops` es read-only status/incidents/help — riesgo menor, pero la auditoría lo nombra.)
2. **Mismatch de Secrets** (`secrets-setup.sh` vs `deployment-agent.yaml`):
   - Deployment lee URL de Secret **`mattermost-webhook`** clave `url`; `WEBHOOK_SECRET` y
     `MM_COMMAND_TOKEN` de Secret **`agent-secrets`** claves `webhook-secret` / `mm-command-token`.
   - El script crea `agent-secrets` con clave `mattermost-webhook-url` (nombre+clave equivocados)
     y **nunca** crea `webhook-secret` ni `mm-command-token` → HMAC/token desactivados en la práctica.

**Fix (dry-run–gated fail-closed, model-agnostic):**
- `_verify_hmac_token`: fail-open permitido **solo** si `remediation_dry_run=True` (dev/test);
  con ejecución real y secret vacío → **rechaza** (`return settings.remediation_dry_run`).
- Slash command: `elif not settings.remediation_dry_run: raise 401` (simétrico, defensa en profundidad).
- Startup: si `not dry_run and not webhook_secret` → log **error** (no warning); idem token.
- `secrets-setup.sh`: crear los 3 secretos con los nombres/claves que el deployment lee de
  verdad (Secret `mattermost-webhook`/`url` + `agent-secrets`/`webhook-secret`+`mm-command-token`),
  comandos en **una línea** (Cloud Shell). Placeholders `<...>`, sin valores reales.
- Tests nuevos: fail-closed cuando `dry_run=False` + secret vacío (callback y slash);
  fail-open preservado con `dry_run=True` (los tests existentes ya lo cubren).

**Por qué gated por `remediation_dry_run` y no un flag nuevo:** `dry_run` YA es el interruptor
"modo real". Un flag extra sería otra cosa que olvidar configurar. Fail-closed automático al
pasar a ejecución real: no puedes remediar de verdad sin auth configurada.

## Hecho
- **`agent/main.py`**:
  - `_verify_hmac_token`: `if not webhook_secret: return settings.remediation_dry_run`
    (fail-open solo en dry-run; fail-closed con ejecución real).
  - `/webhook/command`: rama `elif not settings.remediation_dry_run: raise 401` (simétrico).
  - Startup: log **error** (no warning) cuando `not dry_run and not (webhook_secret|mm_command_token)`.
- **`agent/tests/test_endpoints.py`** (+3 tests): `test_empty_secret_fails_closed_when_not_dry_run`
  y `test_empty_secret_fails_open_when_dry_run` (callback), `test_no_token_fails_closed_when_not_dry_run` (slash).
- **`k8s/secrets-setup.sh`**: reescrito §1 → crea `mattermost-webhook`/`url` (URL) +
  `agent-secrets`/`webhook-secret`+`mm-command-token` (las claves que el deployment lee).
  Comandos en una línea; `webhook-secret` via `openssl rand -hex 32`, `mm-command-token` placeholder.
- `py_compile` + `bash -n` OK. **pytest OK (Jay): 10 passed** (`-k hmac or token or fails_closed or fails_open`).

### P0·3 — rollback durable en Redis (Eje B, 2ª microtarea)
**Problema (auditoría):** `IN_FLIGHT_ROLLBACKS` era un dict en memoria + `asyncio.sleep(300)`.
Si el pod reinicia en esos 5 min (rollout/OOM/spot), el patch queda aplicado y **nunca**
se revierte. Incoherente con el esfuerzo de durabilizar la cola (F2).
**Fix (backstop en Redis, espejo de `escalation_store`):**
- **`agent/rollback_store.py`** (nuevo): `store_rollback`/`delete_rollback`/`list_rollbacks`,
  fail-open, clave `rollback:{id}`, TTL Redis. Mismo patrón que `escalation_store`.
- **`agent/main.py`**:
  - `_rollback_to_dict`/`_dict_to_rollback` (serialize; `asdict(snapshot)` +
    `alert_item.model_dump(mode="json")` + `scheduled_at.isoformat()`).
  - `_schedule_rollback_evaluation`: persiste el ctx en Redis (TTL `timeout*2+grace`)
    además del dict en memoria.
  - `_evaluate_rollback` **refactor**: duerme el **tiempo restante** hasta el deadline
    (`timeout - (now - scheduled_at)`), no el timeout completo → soporta contextos
    recuperados a mitad de espera. `finally` borra también de Redis.
  - `_recover_rollbacks()` nuevo, llamado en el lifespan tras conectar Redis: re-arma en
    memoria + `create_task(_evaluate_rollback)` cada rollback persistido (idempotente:
    salta los ya in-flight; no-op sin Redis).
- **`agent/tests/test_rollback.py`** (+11 tests): `TestRollbackSerialization` (round-trip +
  json), `TestRollbackStore` (store/delete/list + fail-open), `TestRollbackDurability`
  (schedule persiste, evaluate borra, recover re-arma, idempotencia, no-op sin Redis).
- `py_compile` OK. **pytest OK (Jay): 22 passed** en `test_rollback.py` (warnings "coroutine never
  awaited" silenciados: el mock de `create_task` cierra la coroutine). Suite 460→**474**.

## Encontrado / gotchas
- `remediation_dry_run` default `True` en config y **no** lo pisa el conftest → el fail-open
  gated no rompe los tests existentes (`test_no_token_passes_when_secret_unset` sigue 200).
- `/aiops` no tiene subcomando mutante (status/incidents/help) → el riesgo real de P0·2 vive
  en el **callback de botón** (`/webhook/action`), que ejecuta `safe_commands` en "approve".

## Decisiones + por qué
- **Entrada v2 por Eje B (no A):** microtarea barata, sin cluster, cierra un agujero de
  seguridad real → calienta la sesión mientras A (enrichment) merece foco propio. (Elegido por Jay.)
- **No refuse-to-start:** con `DRY_RUN=false` + sin secret, el per-request fail-closed ya
  rechaza; abortar el arranque sería frágil para la demo. Log de error + rechazo por request.

## Siguiente
- **Eje A — `agent/enrichment.py`** (keystone, sesión grande): gather K8s antes del LLM,
  sella slice C / P0·1. Unifica comando humano/auto. → único P0 que queda abierto.
- Validar en cluster: (a) fail-closed (crear secretos con el script arreglado; approve real
  autenticado); (b) rollback durable (matar el pod a mitad de ventana → recover revierte).
- Con P0·2 + P0·3 cerrados, el **Eje B (confianza) está completo** → v2 pasa al keystone.
