# Log de Sesión — 2026-04-30: Fase 4 Evaluación

## Resumen

Sesión de cierre de la fase de evaluación del TFM. Se construyó el módulo `agent/evaluation/` completo
en la sesión anterior. En esta sesión (2026-04-30 continuación) se ejecutaron los scripts contra el cluster.

**Resultado clave obtenido:** `precision@1 = 60%`, `precision@3 = 80%` (eval_retrieval, N=10).
`eval_actionability` no completó por preemptions repetidas de nodos Spot durante la ejecución (~60 min LLM).

## Trabajo realizado

### Paso 0 — Recuperación del cluster (2026-04-30)

El cluster sufrió **2 preemptions de nodos Spot** durante la sesión:
- Primera: detectada al arrancar — Ollama en `Pending`. Pods se reschedularon solos (~5 min).
- Segunda: ocurrió durante la ejecución de `eval_actionability`, matando el port-forward de Ollama.
- Targets de Grafana "down" al inicio = consecuencia de la primera preemption. Se autorrecuperaron.

**Lección operativa:** Los nodos e2-standard-2 Spot se preemptan frecuentemente con carga de inferencia.
Evaluaciones largas (~60 min) son incompatibles con Spot sin retry/resume. El script ya cachea por alerta
(`cached_diagnoses_FECHA.json`) — usar `--use-cache` al reanudar.

### Paso 1 — Módulo `agent/evaluation/` (completado sesión anterior)

**Estructura creada:**
```
agent/evaluation/
  __init__.py
  datasets/
    alerts_oom.json          # 5 payloads KubePodOOMKilled (AlertmanagerPayload válidos)
    alerts_crashloop.json    # 3 payloads KubePodCrashLoopBackOff
    alerts_imagepull.json    # 2 payloads KubePodImagePullBackOff
  ground_truth/
    expected_runbooks.json   # 10 entries: alert_id → runbook YAML stem
  eval_retrieval.py          # Métrica 1: retrieval precision@1 y precision@3
  eval_actionability.py      # Métrica 2: actionability rate + zero-shot vs RAG
  eval_safety.py             # Métrica 3: % SAFE/MUTATING/BLOCKED
  README.md
agent/evaluation_results/
  .gitkeep                   # directorio para outputs JSON
agent/tests/test_evaluation.py  # 15 tests unitarios (mocked)
```

**Decisiones de diseño:**
- Dataset: 10 alertas de 3 tipos (5+3+2). Diversidad: pods/namespaces distintos, variaciones de descripción para que retrieval sea no-trivial.
- Ground truth separado en `expected_runbooks.json` para poder actualizarlo sin tocar los datasets.
- `RUNBOOK_ERROR_CLASSES` map en eval_retrieval.py: filename stem → `error_class` de ChromaDB. Mantiene desacoplamiento entre nombre de fichero y valor del campo.
- eval_actionability cachea en `evaluation_results/cached_diagnoses_FECHA.json` tras cada alerta → permite interrumpir/reanudar sin perder LLM calls (~3 min/alerta).
- eval_safety lee el cache de eval_actionability → sin LLM, instantáneo.
- `--mode both` en eval_actionability genera la comparativa zero-shot vs RAG en una sola pasada.
- ChromaDB en puerto local 8001 (no 8000) para no colisionar con agent-svc en port-forward.

**Reutilización del código existente:**
- `rag.retrieve_context()` y `rag.build_rag_query()` — sin modificar
- `diagnosis.generate_diagnosis()` — sin modificar
- `remediation.validate_commands()` y `CommandSafety` — sin modificar
- `schemas.AlertmanagerPayload` — validación de los payloads del dataset

### Paso 0 — Grafana (pendiente Jay)

Port-forwards y screenshots manuales. Ver plan en `docs_sesion/2026-04-30-plan-evaluacion.md`.

### Paso 2 — eval_retrieval (COMPLETADO 2026-04-30)

**Resultado:** precision@1 = 60% (6/10), precision@3 = 80% (8/10).

**Fallos analizados:**
- `imagepull-001` (miss@1 y @3): query sobre "image not found in registry" → recuperó PodNotReady/ContainerWaiting. El runbook ImagePullBackOff no tiene suficiente overlap semántico con esa descripción.
- `oom-001` (miss@1 y @3): descripción mínima "was OOMKilled in namespace production" → recuperó ContainerWaiting/CrashLoopBackOff. Sin contexto de memory limits, el embedding no discrimina.
- `imagepull-002` (miss@1, hit@3): "batch-processor" + "Registry authentication failed" → JobFailed en rank 1 (semánticamente razonable para un batch job).
- `oom-004` (miss@1, hit@3): "suspected memory leak in batch processing loop" → HighMemory en rank 1 (también razonable).

**Observación clave:** Distancias todas en rango 0.22-0.30, muy comprimidas. Buena recall pero discriminación limitada entre error classes similares. El nomic-embed-text es generalista, no AIOps-específico.

Resultado guardado en: `agent/evaluation_results/retrieval_2026-04-30.json`

### Pasos 3-5 — eval_actionability, eval_safety, docs/10-evaluation.md (COMPLETADOS 2026-04-30)

`eval_actionability --mode both` completado. `eval_safety` completado. `docs/10-evaluation.md` creado.

**Resultados finales:**

| Métrica | RAG | zero_shot |
|---|---|---|
| Retrieval precision@1 | 60% (6/10) | — |
| Retrieval precision@3 | 80% (8/10) | — |
| Actionability | 100% (27/27) | 100% (12/12) |
| Avg confidence | **0.86** | 0.63 |
| Safety SAFE | **100%** (27/27) | 25% (3/12) |
| Safety UNKNOWN | **0%** | 67% (8/12) — alucinaciones |
| Safety BLOCKED | **0%** | 8.3% (1/12) — true positive |

**Nota sobre crashloop-001_rag (error "Server disconnected"):** El primer intento de llamada a Ollama
falló porque el nodo se había reiniciado justo antes de iniciar el script. El port-forward estaba activo
pero Ollama aún estaba en warm-up. Desde crashloop-002 en adelante todo funcionó. Excluida de aggregates.

**Latencia observada:** RAG ~210s vs zero_shot ~40s por alerta — el contexto (3 runbooks + 2 incidents
en el prompt) multiplica ×5 el tiempo de inferencia de qwen2.5:1.5b.

**Hallazgo clave:** El BLOCKED (true positive) y los 8 UNKNOWN son exclusivamente del modo zero_shot.
RAG produce 100% comandos SAFE — solo `describe`, `logs`, `top`. Sin contexto, el LLM alucina
subcomandos inexistentes (`kubectl resize`, `journal-watches`) y propone `kubectl delete` (destructivo).
El validation layer actúa como safety net de segundo nivel.

## Comandos para continuar (próxima sesión)

```bash
# 1. Verificar pods Running antes de empezar
kubectl get pods -n arturo-llm-test

# 2. Port-forwards (terminales separadas)
kubectl port-forward svc/chromadb-svc 8001:8000 -n arturo-llm-test
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test

# 3. Env vars (una línea, misma terminal donde se ejecutan los scripts)
export CHROMADB_HOST=localhost CHROMADB_PORT=8001 OLLAMA_URL=http://localhost:11434/api/generate OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings OLLAMA_MODEL=qwen2.5:1.5b

# 4. Actionability — si el cluster es estable usar --mode both, si no --mode rag primero
cd agent && python -m evaluation.eval_actionability --mode both

# 5. Safety (instantáneo, lee cache del paso 4)
python -m evaluation.eval_safety

# 6. Crear docs/10-evaluation.md con los 3 JSON de evaluation_results/
```

## Archivos modificados / creados

| Archivo | Acción |
|---|---|
| `agent/evaluation/__init__.py` | NUEVO |
| `agent/evaluation/datasets/*.json` | NUEVO — 10 alertas |
| `agent/evaluation/ground_truth/expected_runbooks.json` | NUEVO |
| `agent/evaluation/eval_retrieval.py` | NUEVO |
| `agent/evaluation/eval_actionability.py` | NUEVO |
| `agent/evaluation/eval_safety.py` | NUEVO |
| `agent/evaluation/README.md` | NUEVO |
| `agent/evaluation_results/.gitkeep` | NUEVO |
| `agent/tests/test_evaluation.py` | NUEVO — 15 tests |
| `agent/evaluation_results/retrieval_2026-04-30.json` | NUEVO — precision@1=60%, precision@3=80% |
| `agent/evaluation_results/actionability_2026-04-30.json` | VACÍO — falló por preemption |
| `agent/evaluation_results/cached_diagnoses_2026-04-30.json` | VACÍO — falló por preemption |
| `docs/10-evaluation.md` | PENDIENTE — rellenar con resultados reales |
| `docs/07-roadmap.md` | PENDIENTE — añadir línea evaluación Fase 3 |

## Vault Impact

| Área | Tipo | Acción |
|---|---|---|
| `01_Projects/AIOps-TFG` | Project node | Fase 4 evaluación parcial: retrieval precision@1=60%/@3=80% obtenida. eval_actionability pendiente por preemptions Spot |
| `03_Knowledge/AI_ML/AIOps-Patterns` | Knowledge | Patrón: evaluación offline de sistemas RAG con datasets de alertas simuladas + comparativa zero-shot vs RAG. Lección: evals largas (~60min) incompatibles con Spot sin checkpoint/resume |
