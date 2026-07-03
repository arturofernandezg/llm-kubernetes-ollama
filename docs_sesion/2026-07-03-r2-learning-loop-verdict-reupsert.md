---
fecha: 2026-07-03
slug: r2-learning-loop-verdict-reupsert
promoted: false
---

## Objetivo
Cerrar **R2** (el bucle de aprendizaje RAG real): que el veredicto del rollback
re-escriba el incidente en ChromaDB con su resultado final, para que el retrieval
nunca cite como precedente "bueno" un fix que luego se revirtió.

El objetivo inicial de sesión era **R3** (higiene de query), pero al abrir el
código lo encontramos ya hecho — ver gotchas.

## Hecho
- **`agent/main.py`** — cableado el corazón de R2:
  - Import de `INCIDENT_OUTCOME_{PENDING,CURED,ROLLED_BACK,ROLLBACK_FAILED}` desde `rag`.
  - Métrica nueva `aiops_feedback_verdict_total{outcome}` (cured|rolled_back|rollback_failed):
    distribución de veredictos — señal narrable "¿el auto-fix aguantó?".
  - Helper `_reupsert_incident_outcome(ctx, outcome)`: reusa `ctx.doc_id`+`ctx.remediation`
    (que ya viajaban en el `RollbackContext`, durables en Redis desde el plumbing previo),
    reconstruye el doc con `build_incident_document(..., outcome=...)` y hace `ingest_incident`
    (upsert → actualiza el mismo doc in place). **Fail-open** (try/except interno → nunca
    afecta al rollback ya aplicado; error contado como `FEEDBACK_COUNTER{verdict_failed}`).
  - Ingesta inicial marca `outcome=INCIDENT_OUTCOME_PENDING` (`auto_pending`) **solo cuando
    hay rollback programado** (flag `rollback_scheduled`) — el incidente es provisional hasta
    el veredicto. Sin rollback (o rollback disabled) el comportamiento no cambia.
  - Los 3 veredictos en `_evaluate_rollback` llaman al helper: healthy→`cured`,
    reverted(success)→`rolled_back`, revert_failed→`rollback_failed`.
- **`agent/tests/test_rollback.py`** — clase `TestRollbackVerdictReupsert` (+5 tests):
  cured/rolled_back/rollback_failed re-upsertan el doc con el outcome correcto y reusando
  el `doc_id`; back-compat sin `doc_id` (contexto pre-R2) NO re-upserta pero completa el
  rollback; fail-open si `ingest_incident` revienta (MM se envía, counter healthy sube,
  registro limpio). Helper `_verdict_counter_value`.
- **`docs/07-roadmap.md`** — marcados R2+R3 hechos (fila F4 + detalle R2/R3 + 2 bullets de
  changelog), coherente con cómo el tree ya marcaba R1. *(Esto es capa canónica; se hizo
  aquí por "docs reflect reality" — el resto de promote queda para `/promote`.)*
- **Verificado**: `py_compile` ok; **Jay: 86 passed** en `test_rollback.py`+`test_rag.py`.

## Encontrado / gotchas
- **R3 ya estaba hecho en el working tree, sin loguear ni commitear**: `strip_pod_hash` +
  `_POD_HASH_RE` + 10 tests (`TestStripPodHash`) + `build_rag_query` ya lo usa. Correcto
  (tracé los 10 casos). Regex conservador: solo patrones Deployment (`-<hash5-10>-<rand5>`),
  StatefulSet (`-<ordinal>`), RS/DS (`-<rand5>` con dígito); nunca devuelve vacío (`or pod`).
- **R2 estaba ~70% hecho, también sin loguear**: TODO el plumbing ya existía —
  `RollbackContext.doc_id`+`.remediation`, round-trip Redis + back-compat (`test_backcompat_
  missing_r2_fields`), `build_incident_document(doc_id=, outcome=)`, constantes
  `INCIDENT_OUTCOME_*`, `make_incident_doc_id`, `_remediation_summary`. **Faltaba el corazón**:
  `_evaluate_rollback` decidía healthy/reverted/revert_failed y avisaba a MM, pero **nunca
  re-upsertaba** — `ctx.doc_id`/`ctx.remediation` estaban sin usar ahí. Eso es lo que cerró
  esta sesión.
- Conclusión meta: hay un bloque de trabajo (R2 plumbing + R3) que se hizo en una sesión
  previa sin `/log` ni commit → las bitácoras del 2026-07-03 y el roadmap decían "R3/R2
  pendientes" cuando el tree ya los tenía. Reflejaban intención, no realidad.
- `app.state.http_client`/`chroma_client` disponibles para el re-upsert (main.py:~1231 ya los
  usa para `ingest_incident` en el slash-command). En los tests hubo que primer `app.state`
  con monkeypatch (`raising=False`) porque el lifespan no corre en unit tests.

## Decisiones + por qué
- **Marcar `auto_pending` en la ingesta inicial (no solo re-upsert al final)**: sin ello había
  una ventana (todo el rollback window, + si el re-upsert fallaba) en la que un auto-remediate
  no verificado quedaba registrado como precedente settled-good. La constante
  `INCIDENT_OUTCOME_PENDING` ya existía documentando esa intención; la conducta faltaba.
  Scope algo mayor que "solo cablear _evaluate_rollback" pero es la corrección real del bucle.
- **Helper `_reupsert_incident_outcome` fail-open**: el rollback (revert del patch) es la
  acción crítica ya ejecutada; persistir el veredicto en ChromaDB es secundario. Un ChromaDB
  caído no debe romper ni ensuciar el flujo de rollback. Mismo patrón que la ingesta del
  pipeline (fail-open).
- **Métrica `aiops_feedback_verdict_total` separada de `aiops_feedback_total`**: la primera
  mide la señal de negocio (cuántos auto-fixes curaron vs se revirtieron — gráfica para el
  chapter); la segunda sigue midiendo intentos de persistencia. No mezclar semánticas.
- **Reusar `ctx.remediation` (summary) como arg `remediation` de `build_incident_document`**:
  el summary lleva `execution_log`+`safe_commands` → el doc re-escrito conserva el fix aplicado
  y `fix_applied`; el `outcome` param sobreescribe la acción provisional. Sin `action` en el
  summary, `outcome_value` cae a "unknown" pero el param lo pisa. Coherente.
- **Actualizar docs/07 aquí (no esperar a /promote)**: el roadmap es fuente única de estado y
  ya estaba mintiendo (R2/R3 "pendientes"). "Docs reflect reality". El promote completo
  (CLAUDE.md, vault, docs/10 con la medición A/B) queda pendiente.

## Siguiente
- **Filtrar/exponer `rolled_back` en retrieval** (siguiente slice de R2): el outcome ya se
  persiste, pero el retrieval aún no lo usa — no citar precedentes `rolled_back`/`rollback_failed`
  como buenos, o exponerlos con su etiqueta en el contexto del prompt. + gate de calidad de
  ingesta (contaminación E4).
- **R4** — métrica feedback-loop gain: re-correr `eval_retrieval` con incidents poblados vs
  vacío → gráfica para el deck (si el bucle ya escribe outcomes reales, esto es medible).
- **R3**: medir A/B (`eval_retrieval` con/sin strip) para tener el número — Jay, requiere
  port-forward a ChromaDB+Ollama.
- **Jay**: pytest global + commit del bloque RAG (R1 ya en `f535d42`; R3+R2 + hardening
  pendientes) + los H0 del chapter (recuperación secrets → deploy `cb2d1db` → chaos OOM → Gate 8).
- `/promote` al cerrar el bloque RAG (destilar R2/R3 a CLAUDE.md + vault + docs/10).
