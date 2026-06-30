---
fecha: 2026-06-29
slug: f2-slice4-validacion-cluster
promoted: true
---

> Tercera sesión del 2026-06-29 (tras `f2-slice3-readyz`). Arrancada con `/start`; plan en `~/.claude/plans/smooth-sauteeing-hopper.md`. Jay en el cluster en vivo; yo asisto con comandos one-line y lectura de métricas/logs. Micro-objetivo: **Slice 4 de F2 — validar la cola en cluster** (B happy-path, C replay, D dead-letter, E readyz). Build/deploy/kubectl los lanza Jay.

## Objetivo
Demostrar en el cluster real que la cola Redis Streams (Slices 1-3, hasta hoy solo en código) hace lo que promete: absorbe una ráfaga sin perder alertas, reprocesa lo pendiente al morir el worker (replay), manda los poison messages a cuarentena, y que `/readyz` gated por Redis mantiene el pod en rotación. Primera vez que F1 (PR-01/04/05/06) + F2 (Slices 1-3) se hornean y despliegan.

## Hecho
- **`k8s/deployment-agent.yaml`** (prerequisito de código): env `QUEUE_ENABLED="true"` (activa la cola solo en este deployment; default de `config.py` sigue `False`) + comentario de `readinessProbe` actualizado (modo cola → Redis; legacy → Ollama) + tag de imagen `fd37a5d`→**`c0e6de6`**.
- **Build**: `gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD) .` → SUCCESS, imagen `aiops-agent:c0e6de6`. (Warning conocido de Cloud Logging SA, no bloquea.)
- **Deploy**: `kubectl apply -f k8s/redis.yaml -f k8s/deployment-agent.yaml -n arturo-llm-test` + rollout OK. Logs de arranque confirmaron `Redis connected` → `Consumer group created` (stream `aiops:alerts`, group `aiops-workers`) → `Alert queue enabled` → `Stream consumer loop started`.
- **B (happy-path)** ✅: ráfaga de 10 alertas con fingerprints distintos (`burst-1..10`). `aiops_queue_enqueued_total=10` al instante, webhook 200 inmediato (desacople del LLM). `redis-cli XLEN aiops:alerts=10` (nada perdido), `XPENDING=1` (1 en vuelo + 9 en backlog). `/readyz=200` gated por Redis.
- **C (replay)** ✅: bajado `QUEUE_MIN_IDLE_SECONDS=20`/`QUEUE_RECLAIM_INTERVAL_SECONDS=10` por `kubectl set env` (reinicia el pod → huérfana la entrada in-flight). Resultado: `aiops_queue_reclaimed_total=1` + log `"Pending entry reclaimed and reprocessed", entry_id=...557-0`; `processed{success}=2` pese al reinicio.
- **D (dead-letter)** ✅ (tras reset, ver gotcha): `redis-cli DEL aiops:alerts aiops:alerts:dead` + rollout restart (recrea grupo sobre stream vacío) + `XADD aiops:alerts '*' payload 'not-json' fingerprint poison-1`. Tras ~2 min: `aiops_queue_dead_total=1`; entrada en `aiops:alerts:dead` con forense `orig_id=...534167-0`, `deliveries=4`, `fingerprint=poison-1`; logs: 3 fallos de reclaim (entregas 2/3/4) + `"Entry dead-lettered after 4 deliveries"`.
- **E (readyz)** ✅: `kubectl scale deploy/redis --replicas=0` → `/readyz=503` → reescalado a 1. (La mitad "Ollama caído→200" se SALTÓ por decisión, ver abajo.)
- **Limpieza final**: `kubectl set env deployment/agent QUEUE_MIN_IDLE_SECONDS- QUEUE_RECLAIM_INTERVAL_SECONDS- -n arturo-llm-test`. Verificado `--list | grep QUEUE_` → solo `QUEUE_ENABLED=true` (defaults producción 600/60 restaurados).

## Encontrado / gotchas
- **El backlog bloquea el poison (lección clave).** El 1er intento de dead-letter falló: el `XADD` del poison cayó al final del stream, detrás de ~7-8 alertas reales de B/C que el worker aún masticaba (~205-252s c/u). El `reclaim` SOLO toca el PEL (entregadas-sin-ACK); las **no-leídas** no entran en el PEL → el poison nunca se entregaba → delivery-count 0 → nunca a cuarentena. Solución: aislar el poison en stream pristino (`DEL` + rollout restart para recrear el grupo + `XADD`), así el consumer lo lee de inmediato.
- **`min_idle=600` (10 min) hace el replay/dead-letter NO observables en vivo.** Imprescindible bajarlo (a 20s) + `reclaim_interval` a 10s vía `kubectl set env` para la demo. Seguro porque es test controlado sin tráfico real concurrente que el reclaim pueda robar (el doble-procesamiento at-least-once que documentamos en Slice 2). Revertido al cerrar.
- **Counters Prometheus son por-proceso**: tras reiniciar el pod, `aiops_queue_enqueued_total` volvió a 0 aunque el stream seguía con datos. El dato durable vive en Redis (XLEN/XPENDING), no en el counter. No confundir.
- **Port-forward muere con el pod**: cada rollout/kill rompe el `kubectl port-forward svc/agent-svc`; además el puerto local 18000 a veces no se libera a tiempo y el nuevo PF sale con `exit 1`. Patrón fiable: `pkill -f "port-forward svc/agent-svc"; sleep 2; kubectl port-forward ... & sleep 3; curl readyz`.
- **Dead-letter dispara en `times_delivered > max_deliveries(3)`**, i.e. a la 4ª entrega: consumo(1) + 3 XCLAIM de reclaim (2/3/4). Confirmado por `deliveries=4` en el DLQ.

## Decisiones + por qué
- **Activar la cola por env en el deployment, no flip del default en `config.py`:** valida en cluster sin tocar el comportamiento legacy ni los tests; el default `False` se mantiene hasta retirar el legacy. El legacy queda como red de seguridad durante la validación.
- **Saltar el test "Ollama caído → readyz 200" en vivo:** en modo cola `/readyz` ni llama a Ollama (entra en la rama Redis y retorna) → un fallo de Ollama no puede afectar al readiness por construcción. Escalar Ollama a 0 cuesta minutos de recarga del modelo desde el PVC + ventana de diagnósticos fallidos. El Redis-down 503 + el código ya lo demuestran. (Decisión de Jay.)
- **Reset destructivo del stream para Phase D:** las alertas de B/C ya cumplieron su función; `DEL aiops:alerts` para aislar el poison es más limpio que `XTRIM` (que deja PEL colgando de entradas trimadas → ruido en reclaim). No se pierde nada real.

## Siguiente
- **Commit de manifiestos** (Jay; aplicados al cluster pero sin commitear): `git add k8s/deployment-agent.yaml k8s/redis.yaml && git commit -m "feat: enable Redis Streams queue in cluster (F2 Slice 4) + bump redis to 128Mi"`.
- **Retirar legacy** (ya con la cola validada): quitar `IN_FLIGHT_ALERTS` + `TestInFlightDedup` + camino legacy del webhook en `main.py`, y `queue_enabled=True` por defecto en `config.py`. Rehornear imagen, redeploy.
- **`/promote` de F2 completa** (Slices 1-4): `docs/07` (cerrar F2, changelog, modos de fallo: la línea "se pierde la alerta" pasa a resuelta por la cola), `docs/02` (endpoints/métricas `aiops_queue_*`, readyz condicional), `docs/14` (production-readiness: cola valida durabilidad), `CLAUDE.md` (streams.py, conteo de tests 427, imagen `c0e6de6`), + vault. Marcar las 3 bitácoras del 2026-06-29 como `promoted: true`.
- **Paso F (opcional)**: panel(es) Grafana `aiops_queue_*` (enqueued vs processed, depth, reclaimed, dead) — encaja con Gate 8.
- Pendiente de siempre (cluster): matriz E1–E6 (`docs/14`).
