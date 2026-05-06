# Plan — Sesión 2026-04-30+: Fase 4 Evaluación + Grafana warmup

## Contexto

Estado al 2026-04-30 (última sesión: 2026-04-25):

- **Fase 0** completa (legado).
- **Fase 1** ~98% — pendiente menor: webhook entrante Mattermost (no es esta sesión).
- **Fase 2** completa — RAG + 16 runbooks ingestados, E2E verificado.
- **Fase 3** dry-run activo — reglas 4.5 (bloqueo restart) y 4.6 (≤2× memoria) verificadas E2E. Imagen `c3b0975`. `REMEDIATION_DRY_RUN=true`.

**Por qué esta sesión:** el sistema está al 95%+ funcional. La fase de cierre del TFM/TFG ya no es construir, es **medir**. El roadmap (`docs/07-roadmap.md` § "Estrategia de Evaluación") define 6 métricas para defender el proyecto: MTTR, retrieval precision, actionability rate, safety rate, latencia E2E, feedback loop gain. Ninguna está medida todavía. Sin números, la defensa de la memoria es subjetiva.

**Resultado esperado:** primer reporte de evaluación con cifras reales sobre los 16 runbooks + screenshots Grafana del dashboard que ya está provisionado.

---

## Paso 0 — Warmup Grafana (10 min)

1. `kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring`
2. Abrir dashboard "AIOps Agent — Overview". Verificar que los 9 paneles tienen datos del E2E del 2026-04-24 (mínimo `aiops_remediation_total{action="escalate"}=2` y `aiops_feedback_total{outcome="persisted"}=2` deberían aparecer).
3. Investigar `/metrics` vacío del primer intento de la sesión anterior: `kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test` y luego `curl -s localhost:8000/metrics | grep aiops`. Probable race con buffering de prometheus_client — comparar primer vs segundo curl.
4. Capturar 3-4 screenshots en `docs/screenshots/`:
   - latencia p95 webhook
   - aiops_remediation_total por action
   - aiops_diagnosis_total por outcome
   - targets UP

---

## Paso 1 — Estructura del módulo de evaluación

Crear `agent/evaluation/`:

```
agent/evaluation/
  __init__.py
  datasets/
    alerts_oom.json          # 5 payloads KubePodOOMKilled (formato AlertmanagerPayload)
    alerts_crashloop.json    # 3 payloads CrashLoopBackOff
    alerts_imagepull.json    # 2 payloads ImagePullBackOff
  ground_truth/
    expected_runbooks.json   # mapping alert_id -> runbook esperado top-1
  eval_retrieval.py          # mide retrieval precision
  eval_actionability.py      # mide validez sintáctica de commands[]
  eval_safety.py             # mide tasa de comandos BLOCKED
  README.md                  # cómo correr cada métrica
```

**Reutilizar (no reimplementar):**
- `agent/rag.py` — `query()` y `get_chroma_client()`.
- `agent/diagnosis.py:generate_diagnosis()` — entrypoint del pipeline RAG+LLM.
- `agent/remediation.py:classify_command_safety()` y `validate_commands()`.
- `agent/runbooks/*.yaml` — 16 runbooks como ground truth implícito.
- `agent/schemas.py:AlertmanagerPayload` — para construir los payloads del dataset.
- `agent/tests/helpers.py` — fixtures de payloads pueden servir de semilla.

**Convenciones del dataset:**
- Cada alerta: `{"id": "oom-001", "payload": <AlertmanagerPayload>, "expected_runbook": "oomkilled"}`.
- `expected_runbook` = nombre del fichero YAML sin extensión (match con metadata `error_class`).

---

## Paso 2 — Métrica 1: Retrieval Precision

`eval_retrieval.py`:
- Para cada alerta del dataset, `rag.query(alert_text, top_k=3)` contra ChromaDB poblado.
- Comparar top-1 con `expected_runbooks.json`.
- Reportar `precision@1`, `precision@3`, `recall@3`.
- Output: `evaluation_results/retrieval_FECHA.json`.
- **Comparativa zero-shot vs RAG:** misma batería con `rag.query` mockeado a `[]` → mostrar delta en confidence/commands. Es el dato clave para la memoria.

---

## Paso 3 — Métrica 2: Actionability Rate

`eval_actionability.py`:
- Correr pipeline completo (RAG + LLM) para cada alerta, capturar `diagnosis.commands[]`.
- Por cada comando: ¿empieza por `kubectl `? + `kubectl ... --dry-run=client` valida.
- Output: `evaluation_results/actionability_FECHA.json`.
- **Nota:** lento (~3 min/alerta con el LLM). Cachear diagnósticos en `evaluation_results/cached_diagnoses_FECHA.json` y reusar en eval_safety.

---

## Paso 4 — Métrica 3: Safety Rate

`eval_safety.py`:
- `validate_commands()` sobre `diagnosis.commands[]` cacheados del Paso 3.
- Reportar % SAFE / MUTATING / BLOCKED. BLOCKED debe ser 0%.
- Output: `evaluation_results/safety_FECHA.json`.

---

## Paso 5 — Reporte consolidado

Crear `docs/10-evaluation.md`:

| Métrica | Valor | N | Notas |
|---|---|---|---|
| precision@1 (RAG) | X% | 10 | |
| precision@3 (RAG) | X% | 10 | |
| precision@1 (zero-shot) | X% | 10 | baseline |
| actionability rate | X% | ~30 commands | |
| safety rate (% BLOCKED) | 0% | ~30 commands | |

- Discusión de casos límite.
- Placeholders para MTTR y feedback loop gain (requieren ejecuciones reales acumuladas).
- Actualizar `docs/07-roadmap.md` § Fase 3: añadir línea "Evaluación inicial (FECHA): ver `docs/10-evaluation.md`".

---

## Tests

`agent/tests/test_evaluation.py`:
- `test_dataset_loads` — JSON válidos contra `AlertmanagerPayload`.
- `test_ground_truth_runbooks_exist` — `expected_runbook` referencia YAML real en `agent/runbooks/`.
- `test_eval_retrieval_smoke` — `rag.query` mockeado, verifica cálculo precision@K.

---

## Archivos críticos

| Archivo | Acción |
|---|---|
| `agent/rag.py` | READ ONLY — usar `query()`, `get_chroma_client()` |
| `agent/diagnosis.py` | READ ONLY — usar `generate_diagnosis()` |
| `agent/remediation.py` | READ ONLY — usar `validate_commands()`, `classify_command_safety()` |
| `agent/runbooks/*.yaml` | READ ONLY — ground truth |
| `agent/evaluation/` | NUEVO módulo |
| `agent/tests/test_evaluation.py` | NUEVO |
| `docs/10-evaluation.md` | NUEVO |
| `docs/07-roadmap.md` | UPDATE — añadir línea evaluación Fase 3 |
| `docs_sesion/2026-04-30-evaluation.md` | NUEVO log de sesión |

---

## Verificación end-to-end

```bash
kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring
kubectl port-forward svc/chromadb-svc 8000:8000 -n arturo-llm-test
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test
cd agent && pytest tests/test_evaluation.py -v
cd agent && python -m evaluation.eval_retrieval
cd agent && python -m evaluation.eval_actionability
cd agent && python -m evaluation.eval_safety
cat evaluation_results/retrieval_2026-04-30.json
cat docs/10-evaluation.md
```

**Criterio de éxito:**
- 3 JSON en `evaluation_results/` con cifras reales.
- `docs/10-evaluation.md` con tabla rellenada.
- 3-4 screenshots en `docs/screenshots/`.
- Tests de evaluación pasando.

---

## Vault Impact

| Área | Tipo | Acción |
|---|---|---|
| `01_Projects/AIOps-TFG` | Project node | Actualizar: Fase 4 evaluación iniciada; primeras métricas medidas (precision@1, actionability, safety) |
| `03_Knowledge/AI_ML/AIOps-Patterns` | Knowledge | Patrón: comparativa zero-shot vs RAG como método de evaluación offline para sistemas de diagnóstico |
