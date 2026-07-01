---
fecha: 2026-06-30
slug: eval-model-compare
promoted: true
---

> Continuación directa de `2026-06-30-f3-slice2-auto-cpu`. Tras descubrir en cluster que `qwen2.5:1.5b` no propone `set resources cpu` ante HighCPU, Jay quiso "probar modelos mejores". Esta sesión construye el harness para validarlo **barato y offline** antes de tocar GKE. Código yo; `ollama pull`, runs y commits Jay.

## Objetivo
- Responder *"¿un modelo mejor genera la acción de remediación CPU estructurada (`proposed_action.field=resources.limits.cpu`) que el motor F3 necesita?"* — **sin desplegar nada en cluster** primero. Es decir: convertir la hipótesis "modelo más tocho lo arreglaría" en una medición.

## Hecho
- **`agent/evaluation/eval_model_compare.py`** (script nuevo, reutiliza el harness existente):
  - Recorre `--models qwen2.5:1.5b,qwen3.5,...` y `--alerts <prefijos>` (default `highcpu`).
  - **Inyecta el runbook correspondiente como contexto** leyéndolo de `runbooks/<expected_runbook>.yaml` y replicando la forma de `retrieve_context` (`{id, document, distance, metadata.error_class}`) → **sin ChromaDB ni embeddings**.
  - Por cada (modelo × alerta): `settings.ollama_model = model` → `generate_diagnosis` → pasa el resultado por el **motor real** `validate_commands` + `decide_action` con `remediation_auto_cpu_enabled` **OFF y ON**.
  - Mide lo relevante para F3: `parsed_ok`, `proposed_field`, `field_correct` (¿coincide con el recurso esperado por alertname: HighCPU→cpu, OOM→memory?), `has_set_resources`, `risk`, `confidence`, `verdict_flag_off/on`, `duration_ms`. Imprime tabla por modelo + guarda `evaluation_results/model_compare_<fecha>.json`.
- **`agent/evaluation/datasets/alerts_highcpu.json`** (nuevo): el dataset de evaluación NO tenía HighCPU (solo oom/crashloop/imagepull). Dos variantes: `highcpu-001` mínima (replica la alerta real del cluster: pod `agent-...`, ns `arturo-llm-test`), `highcpu-002` enriquecida (lleva `current resources.limits.cpu: 250m` en la descripción).
- **`agent/evaluation/ground_truth/expected_runbooks.json`**: +2 claves `highcpu-001/002` (additivo, para no romper `eval_retrieval` que hace glob de `datasets/`).
- **Verificación**: `py_compile` OK; smoke sin LLM (carga las 2 alertas, inyecta el runbook `highcpu` con `error_class=HighCPU` doc_len=1380, detección `set resources --limits=cpu=` OK).
- **Comando de run** (Jay, en Mac local): `cd agent && OLLAMA_URL=http://localhost:11434/api/generate .venv/bin/python -m evaluation.eval_model_compare --models qwen2.5:1.5b,qwen3.5`.

## Encontrado / gotchas
- **La arquitectura "modelo en su propio pod" que Jay imaginaba YA existe**: Ollama corre en su Deployment (`ollama-svc:11434`) y el agente le habla por HTTP. Cambiar de modelo = cambiar `OLLAMA_MODEL` + recursos del pod + cargar el modelo al PVC. Cero cambios de código en el agente.
- **El harness de evaluación ya estaba montado** (`agent/evaluation/`: actionability/retrieval/safety, datasets, ground_truth) y `generate_diagnosis` apunta a `OLLAMA_URL` configurable → evaluar contra el Ollama del Mac es una env var, no un harness nuevo. Reutilizado.
- **`qwen3.5` SÍ existe como tag en Ollama** (6.6 GB, `ollama pull qwen3.5` OK) — me equivoqué al dudarlo. El código es agnóstico al modelo igualmente.
- **`pyyaml`**: el python del sistema no lo tiene; el `.venv` sí (`PyYAML==6.0.2` en requirements). Correr el harness con `.venv/bin/python`.
- **El dataset no cubría HighCPU** — había que crearlo para medir nada de CPU. Reaprovechado: `expected_runbook` (campo del dataset) coincide con el *stem* del fichero en `runbooks/` (`highcpu` → `runbooks/highcpu.yaml`), así que la inyección es directa.

## Decisiones + por qué
- **Validar offline en el Mac antes de desplegar un modelo grande** (elegido por Jay sobre "saltar a cargar un 7B al cluster ya"). *Por qué*: cargar un modelo al PVC sin Cloud NAT (crane) + subir RAM de Ollama es caro y lento; sería absurdo descubrir tras todo el esfuerzo que tampoco propone la acción. Mide primero, despliega después. "Construir despacio y bien".
- **Inyectar el runbook en vez de usar RAG real**. *Por qué*: aísla la pregunta — *dado el contexto correcto, ¿el modelo razona la acción CPU?* Si falla con el runbook servido en bandeja, el problema es el modelo/prompt, no el retrieval (que ya se mide aparte en `eval_retrieval`). Además elimina la necesidad de montar ChromaDB + embeddings en local.
- **Pasar por `decide_action` real, no por una métrica proxy**. *Por qué*: el resultado que importa es de negocio (escalate vs auto_remediate), y depende de la cascada completa (4.5/4.6/5/6 + flag). Reportar el veredicto real evita falsos positivos del tipo "propuso algo pero el motor lo bloquea".
- **Dos variantes de alerta (mínima vs enriquecida con el límite actual)**. *Por qué*: separa dos fallos distintos — "el modelo no propone la acción" vs "el modelo no tenía el `current_value` para acotar el `new_value` ≤2×". La alerta real del cluster no lleva el límite, lo que pudo contribuir al fallo observado.

## Siguiente
- **Jay**: lanzar el run en el Mac y pegar la tabla. Lectura: si `qwen3.5` saca `field_ok 2/2` + `auto(on) ≥1/2` → vale el plan de cluster; si sigue `0/2` → el cuello es prompt/runbook (track F4), no el tamaño.
- **Según resultado**:
  - *modelo mejor funciona* → plan de cluster: cargar `qwen3.5`/7B al `ollama-pvc` vía crane (sin Cloud NAT), subir recursos del pod Ollama (un 7B ~5-6 GB; ojo con e2-standard-2 = 8 GB; un 14B probablemente no cabe sin nodo mayor), cambiar `OLLAMA_MODEL`. Re-medir latencia (un 7B es más lento → revisar timeouts y el impacto en la cola F2).
  - *no funciona* → F4: endurecer `runbooks/highcpu.yaml` + `DIAGNOSIS_PROMPT` para empujar `proposed_action(cpu)`; few-shot con un ejemplo CPU en el prompt.
- **Material para el chapter**: la tabla comparativa (tamaño vs `field_ok` vs latencia) es un entregable de presentación por sí mismo.
- **Pendientes arrastrados**: test de integración del camino auto-CPU (slice 2, determinista); decidir sobre el auto-alert HighCPU del propio agente (300m); `/promote` de F3 (slice 1+1b+2) + esta evaluación; matriz E1–E6 (`docs/14`); Gate 8.
