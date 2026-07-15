---
fecha: 2026-07-09
slug: bloque1-quickwins-robustez
promoted: true
---

## Objetivo
Bloque 1 del plan pre-chapter (de 4 bloques acordados hoy): quick wins de robustez
(C-01, C-02, C-03) + cerrar el framing del deck (criterio de 3 patas + borde
fix-correctness). Plan completo: B2 = R5 alertas `resolved` cierran bucle
observacional; B3 = F-17 logs+events al snapshot + C-07 `auth can-i`; B4 = C-08
doble botón + F-06 docs + /ensayo.

## Hecho
- **C-01 (falso-rollback) CERRADO en código**: `check_pod_health` ahora trae
  `lastState.terminated.reason` como 3er campo del jsonpath; un restart solo marca
  unhealthy si el motivo ∈ `_FAILURE_REASONS` (OOMKilled, Error...). Restart benigno
  (`Completed`, p.ej. batch/`stress --timeout`) → healthy + log info. `PodHealthStatus`
  gana `observed_reasons` (default_factory, back-compat). 4 tests nuevos + 2 adaptados
  al formato de 3 campos (`Running|0|;`).
- **C-02 (cooldown en approve) CERRADO**: la rama approve de `main.py` siembra
  `acquire_workload_cooldown` tras ejecución con éxito de una remediación estructurada.
  3 tests nuevos en `TestApproveStructuredParity`. `FakeRedis` gana `.set(nx=, ex=)`
  (espejo redis.asyncio) — sin él, los tests existentes del approve habrían caído por
  el AttributeError (salvados por el fail-soft, pero con ruido).
- **C-03 ya estaba hecho**: `escalation_ttl_minutes=120` en Settings (env-overridable).
  La fila de docs/11 estaba desactualizada — corregida a DONE. Para demos: env
  `ESCALATION_TTL_MINUTES=240` en el deployment, sin tocar código.
- **Deck**: slide motor gana el **criterio de 3 patas** ("el criterio del auto no es la
  alerta, es el arreglo": target confirmable + acción acotada/reversible + resultado
  verificable — solo memoria cumple las tres); slide límites gana card "safe+contexto ✓
  / curación no medida" (safety ≠ correctness); guion §8 ampliado; **2 QA nuevas**
  ("¿por qué solo memoria?" con el criterio, y "¿los comandos del CrashLoop funcionan?"
  con el borde honesto + C-07). Rebuild limpio (demo 2447 KB, 4 capturas embebidas).
- docs/11: filas C-01/C-02/C-03 a DONE con el detalle del fix.

## Decisiones + por qué
- **C-01 — solo la evidencia positiva de fallo dispara rollback**: restart con reason
  vacío/desconocido cuenta como benigno. Alternativa (conservador: unknown → unhealthy)
  descartada porque reintroduce el falso-rollback que motivó C-01, y el coste de un
  rollback perdido es leve (un límite de memoria queda subido; la escalación humana
  sigue existiendo) frente al coste de revertir un fix que curaba. Además, tras un
  patch los pods son nuevos (rolling update) → restartCount>0 post-patch casi siempre
  trae un terminated.reason real.
- **C-02 — sembrar, no gatear**: el humano nunca se bloquea por cooldown (su juicio
  prevalece sobre la ventana); solo se abre la ventana para que el AUTO siguiente del
  mismo workload escale en vez de patchear encima. Fail-soft: si Redis falla en el
  seed, el approve ya ejecutó — no se rompe la respuesta.
- **Criterio de 3 patas al deck**: convierte "solo hace memoria" de limitación en regla
  de diseño (la tesis "el motor dispone" aplicada al alcance). Es la respuesta de
  sistema a dos preguntas hostiles a la vez.

## Encontrado / gotchas
- C-03 llevaba tiempo hecho (Settings 120min) con el backlog sin actualizar — recordatorio
  de que docs/11 también necesita el barrido de /promote.
- `helpers.FakeRedis` no cubría `set(nx=)` — cualquier código nuevo que use SETNX contra
  FakeRedis fallará en silencio (fail-soft) si no se añade el método espejo.

## Siguiente
1. **Jay**: `pytest` global + validar deck en pantalla (slides motor + límites) + commit:
   `git add agent/remediation.py agent/main.py agent/tests/test_remediation.py agent/tests/test_endpoints.py agent/tests/helpers.py scripts/build_demo.py demo/demo.html demo/guion.html docs/11-quality-backlog.md docs_sesion/2026-07-09-bloque1-quickwins-robustez.md && git commit -m "fix(remediation): C-01 falso-rollback (lastState.reason) + C-02 cooldown en approve + deck criterio 3 patas + QA no-memoria"`
2. **Bloque 2 (R5)**: alertas `resolved` → correlar por fingerprint → outcome
   `resolved_observed` en ChromaDB + métrica `aiops_incident_resolution_seconds{error_class}`.
3. Bloques 3-4 según plan (F-17 + C-07; luego C-08 + F-06 docs + /ensayo).
4. Arrastrados: commit de ayer (demo R4) sigue pendiente; matriz E1-E6; Gate 8; /promote masivo.

## Vault Impact
| Archivo | Cambio |
|---|---|
| 03_Knowledge/AI_ML/ (patrón) | Criterio de 3 patas para auto-remediación (confirmable + acotado/reversible + verificable) — generalizable a cualquier agente que actúe sobre infra |
| 01_Projects/AIOps node | Bloque 1 pre-chapter cerrado; plan 4 bloques |
