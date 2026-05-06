# Módulo de Evaluación — AIOps Agent

Mide las 3 métricas offline del TFM: retrieval precision, actionability rate y safety rate.

## Requisitos previos

```bash
# Terminal 1: ChromaDB (puerto local 8001 para no colisionar con agent-svc en 8000)
kubectl port-forward svc/chromadb-svc 8001:8000 -n arturo-llm-test

# Terminal 2: Ollama
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test
```

Variables de entorno necesarias:

```bash
export CHROMADB_HOST=localhost
export CHROMADB_PORT=8001
export OLLAMA_URL=http://localhost:11434/api/generate
export OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings
export OLLAMA_MODEL=qwen2.5:1.5b
```

## Ejecutar las métricas

Desde el directorio `agent/`:

```bash
# Métrica 1: Retrieval Precision (~30 segundos, solo embeddings)
python -m evaluation.eval_retrieval

# Métrica 2: Actionability Rate (~30 min total, llama al LLM 10 veces)
python -m evaluation.eval_actionability --mode rag

# Métrica 2b: Comparativa zero-shot vs RAG (añade otros ~30 min)
python -m evaluation.eval_actionability --mode both

# Reusar cache del día (para re-run rápido o después de interrumpir)
python -m evaluation.eval_actionability --mode rag --use-cache

# Métrica 3: Safety Rate (usa cache de eval_actionability, instantáneo)
python -m evaluation.eval_safety
```

## Datasets

| Archivo | Tipo | N |
|---|---|---|
| `datasets/alerts_oom.json` | KubePodOOMKilled | 5 |
| `datasets/alerts_crashloop.json` | KubePodCrashLoopBackOff | 3 |
| `datasets/alerts_imagepull.json` | KubePodImagePullBackOff | 2 |

Ground truth: `ground_truth/expected_runbooks.json` (mapping alert_id → runbook YAML stem).

## Outputs

Guardados en `agent/evaluation_results/`:

| Archivo | Contenido |
|---|---|
| `retrieval_FECHA.json` | precision@1, precision@3, detalle por alerta |
| `actionability_FECHA.json` | actionability_rate, avg_confidence, detalle por alerta |
| `cached_diagnoses_FECHA.json` | Diagnósticos cacheados (input para eval_safety) |
| `safety_FECHA.json` | % SAFE/MUTATING/BLOCKED por comando |

## Tests

```bash
cd agent && pytest tests/test_evaluation.py -v
```
