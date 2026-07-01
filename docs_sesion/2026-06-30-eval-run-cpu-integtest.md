---
fecha: 2026-06-30
slug: eval-run-cpu-integtest
promoted: true
---

> Continuación directa de `2026-06-30-eval-model-compare` (que construyó el harness). Esta sesión (1) cierra el test de integración determinista del camino auto-CPU y (2) **ejecuta** el eval de modelos, con un resultado que **da la vuelta** al hallazgo de slice 2. Arrancada con `/start` → micro-objetivos: "test integración auto-CPU" + "lanzar el eval". Código y tests yo; `ollama pull`, runs y git Jay.

## Objetivo
- **Test de integración auto-CPU (F3 slice 2)**: recorrer `process_remediation()` entero con un `proposed_action(cpu)` mockeado y el flag off/on, demostrando que las piezas se enganchan (validate → decide → capture → execute → revert). Determinista, sin cluster ni LLM.
- **Ejecutar el eval de modelos**: convertir la hipótesis "un modelo más potente arreglaría el camino CPU" en una medición offline antes de tocar GKE.

## Hecho
- **`agent/tests/test_remediation.py`**: nueva clase `TestProcessRemediationCpuAuto` (3 tests, cero cambios en `remediation.py` — esto es cobertura, el motor ya existía):
  - `test_flag_off_escalates_without_effects`: flag off → `ESCALATE`, `pre_patch_snapshot is None`, `execute_results == []`, `execution_attempted is False`.
  - `test_flag_on_auto_remediates_with_cpu_snapshot`: flag on (dry-run) → `AUTO_REMEDIATE`, `snapshot.value=="250m"`, `snapshot.field=="resources.limits.cpu"`, `--limits=cpu=500m` en los comandos ejecutados.
  - `test_rollback_from_snapshot_reverts_cpu`: `revert_patch(snapshot)` → `--limits=cpu=250m` + `"memory" not in command` (no regresión memory-céntrica).
  - Reutiliza el patrón de monkeypatch de `TestProcessRemediationSnapshot` + `mock_diagnosis_auto_remediate()` como base. `py_compile` OK. Pendiente pytest de Jay (esperado: fichero 165→168, suite 422→425).
- **Eval ejecutado** (`agent/evaluation/eval_model_compare.py`, dos pasadas): primera con `qwen2.5:1.5b` sin cargar (404), segunda tras `ollama pull qwen2.5:1.5b` con ambos modelos. Resultado en `agent/evaluation_results/model_compare_2026-06-30.json`.

## Encontrado / gotchas
- **HALLAZGO PRINCIPAL — se refuta slice 2: NO era el modelo, era el contexto.** Con el runbook inyectado, **`qwen2.5:1.5b` acierta el field CPU 2/2** (`resources.limits.cpu` + `set resources cpu` real) en **8.9s** de media. El fallo en cluster de slice 2 ("el modelo no propone CPU") no fue capacidad del modelo, sino **falta de contexto**: el retrieval no surfaceó el runbook HighCPU y/o la alerta no llevaba el límite actual. → La palanca es **F4 (retrieval + runbook + enriquecer alerta)**, no un modelo mayor.
- **`qwen3.5` descartado**: 147s de media (uno **294s ≈ 5 min**), **1 de 2 errró** (error vacío, probable fallo de generación/parse), y tampoco auto-remedia. Inviable para la cola F2 en un cluster con menos CPU que el M4. El tamaño de modelo es la palanca equivocada.
- **`auto(on)=0/2` en ambos — la validation layer haciendo su trabajo**, cada caso cae por un gate distinto y legítimo:
  - qwen2.5/highcpu-001: la alerta mínima no lleva el límite → el modelo **alucina `current=1000m`** y propone `5000m` → bloqueado por el **cap 2×** (regla 4.6). Defensa funcionando.
  - qwen2.5/highcpu-002: `conf=0.70` < 0.9 → la excepción 4.5 lo deniega.
  - qwen3.5/highcpu-002: `risk=low` ✅ pero `conf=0.85` < 0.9 → excepción 4.5 también lo deniega.
  - Lección: "flip del flag" por sí solo nunca basta. Auto-CPU exige **conf≥0.9 + risk≤medium + bump≤2×**. Ventana estrecha por diseño.
- **El umbral de confianza de la excepción 4.5 (≥0.9) es más estricto que el auto general (≥0.8)** — por eso un diagnóstico "bueno" (risk=low, conf=0.85) aún escala. Es coherente (set-resources reinicia pods → más exigente), pero conviene tenerlo presente: el cuello realista del auto-CPU no es el flag ni el field, es alcanzar conf≥0.9.
- **404 en la primera pasada**: `qwen2.5:1.5b` no estaba en el Ollama del Mac. La fila salió `0/2` como artefacto, no como medición. Fix: `ollama pull qwen2.5:1.5b` y re-correr. (Recordatorio: el harness reporta `error` por celda, no rompe el run entero.)
- **El eval inyecta el runbook en bandeja** → prueba "dado el contexto correcto, ¿el modelo es capaz?" (sí). El gap de cluster es otro: **¿el retrieval entrega ese runbook?** Esa es la pregunta de F4, medible con `eval_retrieval` (p@1 del HighCPU).

## Decisiones + por qué
- **Pivotar de "modelo mayor" a F4 (calidad de retrieval/contexto)**. *Por qué*: el eval mató la hipótesis del modelo grande con datos — qwen3.5 es más lento, más frágil y no mejora el outcome; el pequeño ya es capaz y rápido cuando recibe el contexto. Gastar en GPU/RAM sería resolver el problema equivocado. "Business value > tech hype", "construir despacio y bien": medir antes de desplegar evitó cargar un 7B inútil al PVC.
- **No relajar los umbrales de la excepción CPU para forzar auto(on)>0**. *Por qué*: los tres bloqueos son correctos (cap 2× atrapó una alucinación; conf<0.9 frena diagnósticos tibios). Bajar el listón para "demostrar" auto-remediación sería deshonesto e inseguro. El mecanismo es correcto; la activación real depende de un diagnóstico genuinamente confiable y acotado.
- **El test de integración va por `process_remediation` y `revert_patch` reales, no proxies**. *Por qué*: el valor es demostrar el enganche end-to-end de las piezas que ya estaban testeadas por separado (decide/capture/revert), cerrando la cobertura del camino auto-CPU sin depender del LLM.

## Siguiente
- **F4 — atacar el contexto, que es el cuello real**:
  1. **Medir el retrieval de HighCPU** en cluster (`eval_retrieval`, p@1/p@3): ¿surfacea `runbooks/highcpu.yaml`? Era el eslabón roto en slice 2.
  2. **Enriquecer la alerta HighCPU con el límite de CPU actual** (regla Prometheus / payload): sin él el modelo alucina el `current_value` (`1000m→5000m`); con él propone un bump sano ≤2×. Cambio barato y de alto impacto.
- **Material para el chapter**: la tabla (tamaño vs field_ok vs latencia) + la narrativa "lo medimos y era retrieval, no capacidad" es un entregable por sí mismo.
- **Pendientes arrastrados**: `/promote` de F3 (slice 1+1b+2) + este eval + el test de integración → docs 02/07/CLAUDE.md, conteo de tests (→425), imagen `4534447`, e incluir como modo de fallo/nota de honestidad el "auto-CPU bloqueado por conf≥0.9, no por el flag"; matriz E1–E6 (`docs/14`); Gate 8; decidir sobre el auto-alert HighCPU del propio agente (limits 300m).
