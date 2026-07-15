---
fecha: 2026-07-12
slug: promote-unificacion-ruta-chapter
promoted: true
---

> Esta bitácora documenta el propio `/promote` masivo — nace `promoted: true` porque su
> contenido YA está en los docs canónicos (es el registro de esa consolidación).

## Objetivo
Petición de Jay: "unifica los docs de progreso en uno" + analizar estado del repo y los HTML
del deck + proponer ruta de mejora a 3 días (chapter 15-jul). Decisiones tomadas vía plan
aprobado: (1) unificación = `/promote` masivo con `docs/07` como fuente única (NO doc nuevo,
NO fusión 07+11+14); (2) SÍ se toca el cluster el 13-jul (deploy `588e3a9` + 1 arco chaos
para captura C-08 + Gate 8); (3) matriz E1-E6 → cierre honesto en docs sin experimentos.

## Hecho
- **`/promote` masivo** (7 bitácoras reales `promoted: true` en frontmatter: 07-07 promote-plan,
  07-08 r4, 07-09 ×2, 07-10 ×3 — el grep de cuerpo da falsos positivos, filtrado con `awk FNR<6`):
  - `docs/07`: fecha 07-12; estado actual gana F-17 (bloque grounding), C-07+C-08 (bloque ChatOps),
    bullet nuevo R5; tests **691 funciones / 17 ficheros / pytest colecta 696** (verde 07-11);
    nota explícita "el cluster corre `0914611` SIN R5/F-17/C-07/C-08"; sprint S4→🟡 (queda Gate 8),
    S5→🟡 (queda ensayo), S6→✅ (F-06 vía docs), **S7 nuevo** (deploy+chaos 13-jul), **S8 nuevo**
    (ensayo+plan B 14-jul); 4 entradas de changelog (deck v3 · C-08 · R5+F-17+C-07 · R4+quick-wins);
    modos de fallo: C-01 y C-07 → ✅ resuelto, fila nueva F-06 (claim Redis degradado).
  - `CLAUDE.md`: estado (696, C-08, deck v3, gap deploy), "En curso" → ruta 3 días, archivos clave
    (remediation/mattermost C-08, config F-17+R5, tests 691/17, build_demo_v3, demo/ v3).
  - `docs/06`: recuentos por fichero (endpoints 137, remediation 245, enrichment 35, diagnosis 31),
    +2 filas (`test_incident_index.py` 15, `test_eval_feedback.py` 16), total 691/696, +6 entradas
    de changelog (R4 harness → C-08).
  - `docs/11`: C-08 → ✅ DONE (HMAC por acción); F-06 → ✅ DONE por vía docs.
  - `docs/02`: tabla de módulos (+`incident_index.py`, enrichment F-17, remediation C-07/08,
    mattermost C-08), webhook/alert rama resolved R5, tabla de acciones del callback
    (+`approve_engine`/`approve_model`), métricas R5.
  - `docs/10` y `docs/12`: verificados al día, sin cambios.
  - 7 bitácoras → `promoted: true`.
- **F-06 cerrado por vía docs (S6)**: claim de durabilidad degradado y declarado — durable ante
  reinicio del AGENTE, no ante muerte del pod REDIS; fila en modos de fallo (07), nota en 14,
  coherente con la QA 7 del guion v3. AOF+PVC → v2.1.
- **Matriz E1-E6 cerrada honesta (docs/14)**: E4/E4b/E5 → ✅ respondidos por F2 (validación en
  cluster 06-29); E1/E2/E2b/E3/E3b/E6 quedan como protocolo definido no ejecutado, declarado en
  el header. PR-01..07 todos resueltos.

## Encontrado
- **El recuento real es 691 funciones / 17 ficheros** (contado con grep); pytest colecta 696
  (parametrize ~5). El deck v3 ya decía 696; docs/07/CLAUDE.md/06 decían 621 — 6 días de drift.
- **`agent/evaluation_results/safety_2026-07-11.json` es un artefacto de pytest** colado en el
  repo (total_commands=2, `cache_file` apunta a un tmp de pytest) — no es un eval real como los
  demás safety_*.json. Borrar (comando abajo).
- **RBAC ya cubre F-17** (`pods/log` get + `events` get/list en `k8s/rbac.yaml`) — el gather de
  logs/events funcionará en cluster sin tocar permisos. Verificado antes de planificar el run S7.
- `.DS_Store` está gitignored y NO trackeado (el finding A4 de docs/11 sobre esto queda a medias:
  lo del binario de producción sigue TODO).

## Decisiones + por qué
- **Unificación = /promote a docs/07, no doc nuevo**: el método ya define 07 como fuente única;
  crear otro doc de estado duplicaría la verdad (y la fusión literal 07+11+14 rompería los
  ficheros atómicos / context-window budget). Elegido por Jay entre 3 opciones.
- **Deploy S7 el 13-jul a pesar de estar a 2 días**: riesgo bajo porque la demo es replay (cero
  dependencia del cluster) y hay rollback trivial a `0914611` validado; el retorno es evidencia
  visual real (captura C-08 doble botón + métrica R5 viva + Gate 8). Elegido por Jay.
- **E1-E6 sin correr**: coherente con el tono "prototipo honesto" del deck — mejor declarar
  protocolo-definido que veredictos con prisa. Elegido por Jay.
- **F-06 por la vía docs**: montar AOF+PVC a 3 días del chapter es riesgo sin retorno narrable;
  la QA 7 del guion ya respondía honesto — los docs ahora dicen lo mismo.

## Siguiente
1. **Jay — commit de hoy** (una línea, sin Co-Authored-By):
   `git rm agent/evaluation_results/safety_2026-07-11.json && git add -A && git commit -m "docs(promote): consolidación pre-chapter — 696 tests, R5/F-17/C-07/C-08, deck v3, F-06 claim degradado, E1-E6 cierre honesto"`
2. **13-jul (S7)**: `/ship` de `588e3a9` (tag = short SHA, NUNCA build ID) + `/chaos-run` arco OOM
   → captura C-08 (doble botón MM) + R5 (`aiops_incident_resolution_seconds` en Grafana) + Gate 8
   screenshots; teardown = último gate; opcional embeber la captura en el deck v3
   (`scripts/build_demo_v3.py` + regenerar). Contingencia: quedarse en `0914611`.
3. **14-jul (S8)**: `/ensayo` (16 QA guion v3) + timing ~15 min + PDF Ctrl+P + copia móvil/USB +
   **vault end-session** (Vault Impact acumuladas de las bitácoras promovidas hoy).
4. **15-jul**: checklist mañana (offline, N/F, respaldos R1-R4 localizados).

## Vault Impact
| Archivo | Cambio |
|---|---|
| 01_Projects/AIOps node | Estado 07-12: código completo (691/696 tests), docs consolidados, ruta 3 días a chapter (S7 deploy+evidencia, S8 ensayo) |
| 04_Systems/ (patrón método) | El deck es un doc más del barrido de staleness: el v1 llegó a vender como abierto lo ya cerrado — "docs reflect reality" aplica también a slides |
| 03_Knowledge/Programming/ (patrón, pendiente de las bitácoras 07-10) | HMAC por acción en callbacks multi-opción · jerarquía señal fuerte/débil · doble botón model/engine — ejecutar en el vault end-session del 14-jul |
