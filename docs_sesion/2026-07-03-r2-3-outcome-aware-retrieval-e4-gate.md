---
fecha: 2026-07-03
slug: r2-3-outcome-aware-retrieval-e4-gate
promoted: false
---

## Objetivo
Cerrar **R2·3** — el consumo del bucle de aprendizaje: que el retrieval USE los
outcomes que R2 ya persiste (no citar fixes sin veredicto, no repetir fixes
revertidos) + el **gate de calidad de ingesta** (prevención del finding E4 de
`docs/11`, contaminación del RAG).

## Hecho
- **`rag.py`**:
  - `INCIDENTS_RETRIEVAL_FILTER = {"outcome": {"$ne": "auto_pending"}}` aplicado a la
    query de incidents en `retrieve_context`. **Sin fallback** (a diferencia del
    two-stage de runbooks): incidents vacío es estado normal y un fallback anularía
    el filtro.
  - `incident_worth_ingesting(diagnosis)` — gate E4: confidence ≤ 0 / ausente /
    no-numérica → no ingesta. Cubre el parse failure del LLM (`generate_diagnosis`
    pone confidence=0.0), que hoy entraba a ChromaDB como precedente basura.
  - `build_incident_document`: `error_class` pasa del alertname crudo
    (`KubePodOOMKilled`) a la clase mapeada (`error_class_for_alertname` → `OOMKilled`)
    — mismo vocabulario que runbooks.
- **`diagnosis.py`**:
  - `format_context_docs`: si el metadata trae `outcome`, header
    `[clase | outcome: X]`; para `rolled_back`/`rollback_failed` añade
    `— FAILED FIX (reverted, do not repeat)`. Runbooks (sin outcome) intactos.
  - `DIAGNOSIS_PROMPT`: regla nueva — "cured = funcionó; rolled_back/rollback_failed
    = fix aplicado que FALLÓ; NEVER propose a fix marked as failed again".
  - Import de las constantes de outcome desde `rag` (sin ciclo: rag no importa diagnosis).
- **`main.py`**: gate E4 cableado en el feedback loop (`elif not
  incident_worth_ingesting(diagnosis)` → counter `aiops_feedback_total{skipped_low_quality}`
  + log, sin ingesta). NO aplica al re-ingest de decisión humana (~línea 1273) — un
  approve/reject humano es señal de calidad por sí misma.
- **Tests +16**: `test_rag.py` (5 `TestIncidentWorthIngesting`, 4
  `TestIncidentsOutcomeFilter` — shape del filtro, where en la query, filtro-miss sin
  fallback, rolled_back sigue recuperable —, 1 identidad error_class; assertion de
  `test_metadata_has_required_fields` ajustada a `OOMKilled`); `test_diagnosis.py`
  (5: labeling cured/rolled_back/rollback_failed, runbook sin outcome intacto, guard
  del contrato del prompt); `test_endpoints.py` (1: pipeline con confidence 0.0 → no
  ingesta, MM se envía igual). `py_compile` OK ×6.
- **`docs/07`** actualizado (fila F4 + detalle R2 + bullet changelog R2·3) — "docs
  reflect reality"; el resto queda para `/promote`.

## Encontrado / gotchas
- El test `test_pending_constant_value` de la sesión anterior ya anticipaba este slice
  ("Guard the wire value the retrieval filter (R2·3) will key on") — el diseño estaba
  pre-acordado: filtrar por el wire value `auto_pending`.
- Semántica `$ne` de ChromaDB: un doc SIN clave `outcome` no matchea `$ne` (queda
  excluido). Todos los docs de incidents los escribe `build_incident_document` (siempre
  pone outcome) y la purga E4 de 2026-05-27 limpió los legacy → riesgo aceptado. Si
  algún día se ingesta por otra vía, cuidado.
- Los incidents pre-R2 en el cluster tienen outcomes tipo `auto_remediate`/`resolved`/
  `no_remediation`: pasan el filtro (correcto) y ahora aparecen con
  `| outcome: auto_remediate` en el header del prompt — más señal, no rompe nada.
- `RemediationAction` se importa a nivel de módulo pero al FINAL de
  `test_endpoints.py` (línea ~1749) — disponible en el namespace del módulo en runtime,
  el test nuevo lo usa sin import local.

## Decisiones + por qué
- **Trato asimétrico de outcomes** (la bitácora anterior dejaba abierto "filtrar O
  exponer"): `auto_pending` se FILTRA (un fix sin veredicto no es precedente, es ruido
  transitorio; el cooldown F-01 ya evita el re-patch, no se pierde nada); `rolled_back`/
  `rollback_failed` se EXPONEN etiquetados (conocimiento negativo: "ya probamos 2× y no
  curó" debe empujar al LLM a otra vía/escalar, no desaparecer — y es LA parte narrable:
  el sistema recuerda sus fracasos). Filtrarlo todo habría sido más simple pero perdía
  la señal.
- **Gate E4 sin umbral configurable** (solo `> 0.0`): determinista y sin números
  inventados. Un diagnóstico parseado con confidence baja sigue siendo historia legítima
  (y la confidence ya viene grounded del Eje A); el único caso indefendible es el
  parse failure / cero explícito. Si en el futuro hace falta umbral, se promociona a
  setting.
- **`error_class` mapeado en incidents**: coherencia de vocabulario con runbooks (R1),
  headers correctos en el prompt, y habilita filtro por clase en incidents más adelante
  sin migración de esquema. Los docs legacy con alertname crudo no rompen nada (el
  filtro nuevo solo mira `outcome`).
- **Regla en el prompt además del label**: con qwen2.5:1.5b no basta con etiquetar el
  contexto — el modelo necesita la semántica explícita en RULES. El test
  `test_prompt_instructs_model_about_failed_fixes` guarda ese contrato (label y regla
  deben evolucionar juntos).

## Siguiente
- **R4** — métrica feedback-loop gain: re-correr `eval_retrieval` con incidents
  poblados (outcomes reales) vs vacío → gráfica para el deck. Requiere cluster (Jay).
- **Jay**: pytest global (esperable 573+16=589 en 15 ficheros) + commit del bloque RAG
  (R3+R2+R2·3+gate E4; R1 ya en `f535d42`) + los H0 del chapter (recuperación secrets →
  deploy → chaos OOM del arco completo → Gate 8).
- Chaos OOM ahora también valida R2·3: tras el veredicto, comprobar que el incident en
  ChromaDB tiene el outcome final y que un segundo incidente igual recibe el contexto
  etiquetado (`FAILED FIX` si hubo rollback).
- `/promote` al cerrar el bloque RAG (CLAUDE.md `agent/rag.py`/`diagnosis.py`, docs/10,
  vault).

## Vault Impact
| Fichero vault | Cambio |
|---|---|
| `01_Projects/AIOps_Agent.md` | R2·3: retrieval outcome-aware (asimetría filtrar pending / exponer failures etiquetadas) + gate E4 |
| `03_Knowledge/AI_ML/RAG_Feedback_Loops.md` | Patrón: negative knowledge en RAG — failed fixes como precedente etiquetado, no filtrado; pending sí filtrado (asimetría veredicto/no-veredicto) |
