---
fecha: 2026-07-08
slug: r4-feedback-loop-harness
promoted: true
---

## Objetivo
Arrancar **R4 (feedback-loop gain)**: la métrica/gráfica estrella del deck que demuestra que el
bucle de aprendizaje RAG (R2/R2·3) aporta valor. Diseñar un experimento **controlado y legítimo**
(no marketing) y dejar el harness listo para que Jay lo corra en cluster.

## Hecho
- **Hallazgo que reencuadra R4** (lo más importante de la sesión): el plan del roadmap —"re-correr
  `eval_retrieval` con incidents poblados vs vacío"— **no puede mover el número**. `eval_retrieval.py`
  puntúa **solo la colección `runbooks`** (`context["runbooks"]`); `incidents` es una colección
  separada que ese script nunca lee. Poblar incidents → línea plana. La decisión "filtrar vs limpiar
  los ~30 docs del horno nocturno" que el roadmap marcaba como precondición era **moot** para ese
  script.
- **Reencuadre a dos capas** (harness nuevo `agent/evaluation/eval_feedback.py`):
  - **Capa A** — precisión de retrieval de *incidents* (embedding-only, N=15, ~30s): ¿la query de
    incidents devuelve un incidente de la misma `error_class` en top-k? Vacío (medido) vs fixture
    poblado. Claim acotado: "la memoria surge a la clase correcta", NO "mejora el diagnóstico".
  - **Capa B** — ablación de conocimiento negativo (LLM, 3 arms, la slide de rigor): misma alerta
    OOM estímulo (`oom-002`); arm0 vacío / arm1 incident `rolled_back` / arm2 *mismo texto* `cured`.
    Resultado limpio = arm1≠arm0 ∧ arm2≈arm0 → la etiqueta de outcome es causal. Métrica a nivel de
    **diagnóstico** (`proposed_action`/confidence/menciona-fallo), NO del motor.
- **Fixture declarado** `agent/evaluation/fixtures/incidents_seed.json`: 10 incidents sintéticos en 5
  clases (Capa A) + base de la ablación (Capa B). `_meta.disclosure` marca que son sintéticos.
- **Protocolo PRE-REGISTRADO** en `docs/10-evaluation.md` (sección 2026-07-08, marcada "PENDIENTE DE
  CORRER"), con la **hipótesis nula explícita** que Jay pidió anotar: que el 1.5b **ignore** el
  conocimiento negativo → se reporta tal cual (haría falta modelo superior o guard determinista en el
  motor). Mecanismo verificado en código: `diagnosis.py:33` (regla "never propose a fix marked as
  failed") + `format_context_docs` (`diagnosis.py:92`, etiqueta "FAILED FIX — do not repeat").
- **Tests offline** `agent/tests/test_eval_feedback.py` (fixture, extracción de outcome, retrieval de
  clases mockeado, fallback runbooks two-stage). `py_compile` OK + validación JSON del fixture OK
  (no corrí pytest — lo corre Jay).
- **Handoff** a Jay: port-forwards (chromadb-svc 8001:8000, ollama-svc 11434:11434) + comando de run
  (`python -m evaluation.eval_feedback --reps 3`, ~30 min) + one-liner de investigación del horno
  nocturno + comando pytest.

## Encontrado / gotchas
- **`eval_retrieval` no medía el bucle** (arriba). Lección: verificar QUÉ mide un harness antes de
  planificar una medición sobre él; el roadmap arrastraba una suposición de cuando el eval no existía.
- **Confound del motor en el eval**: sin pod real → `snapshot=None` → el seal marca `target_unresolved`
  → `decide_action` escalaría **uniformemente** en los 3 arms (regla 4.7), lavando la señal. Por eso la
  métrica de Capa B se mide a nivel de diagnóstico del LLM, no del verdict del motor. En prod, un
  `proposed_action` caído o confidence bajo umbral es lo que enruta a ESCALATE.
- **Sin venv en el repo** (Jay usa su entorno; `httpx` no está en el python del sistema) → no pude
  hacer smoke import completo; me quedé en `py_compile` + validación del JSON.
- **Pods en cluster (reportado por Jay al final): uno `Succeeded`, otro `Pending`.** Sin diagnosticar
  aún. `Succeeded` en un servicio long-running (ollama/chromadb) es anómalo (el contenedor salió 0);
  `Pending` = no schedulado (posible espera de nodo `guaranteed=true`, recursos, o preemption de Spot).
  **Bloquea el run de R4 si el Pending/Succeeded es ollama o chromadb** (el eval los necesita Ready).
  Pendiente: `kubectl get pods -n arturo-llm-test -o wide` + `kubectl describe pod <pending>` para ver
  el motivo (Events).

## Decisiones + por qué
- **R4 = experimento controlado, no curva de precisión de producción**: con N real minúsculo (~2
  incidents), la credibilidad viene del **diseño**, no del tamaño de muestra. Palancas: pre-registro,
  una sola variable entre arms, `temp=0`, **ablación cured-vs-rolled_back** (aísla la CAUSA: la
  etiqueta, no "memoria vs no-memoria"), fixture sintético declarado y auditable, reporte por-caso,
  disposición a publicar el null.
- **Métrica a nivel de diagnóstico, no de motor** (por el confound del snapshot=None). Mide exactamente
  el claim (¿usa el LLM el conocimiento negativo?) sin ruido del motor.
- **Colección desechable + teardown en `finally`**: prod `incidents` intacta (lección horno nocturno:
  teardown = último gate). Esto además hace la limpieza del horno nocturno **no bloqueante** para el
  eval (aunque sigue recomendada por higiene de prod: los ~30 HighCPU free-text se citarían como
  precedente basura en HighCPU reales).
- **Fixture del arm negativo por flip de etiqueta** (sintético), no dependiente de un `rolled_back`
  real: la ablación es MÁS limpia con texto idéntico salvo la etiqueta. Jay puede sustituir un real si
  lo tiene, pero no es necesario.
- **Escribí el protocolo en `docs/10` ya** (no esperé a /promote): la pre-registración ES el artefacto
  científico y su valor depende de existir ANTES del run. Marcado "PENDIENTE DE CORRER" para no
  confundirlo con un resultado.

## Siguiente
1. **Diagnosticar los pods** (`Succeeded`/`Pending`) en `arturo-llm-test` antes de nada:
   `kubectl get pods -n arturo-llm-test -o wide` + `kubectl describe pod <pending>` (mirar Events:
   scheduling, nodo guaranteed, Spot). Si el Pending es ollama/chromadb, el run de R4 no arranca.
2. **Correr R4** (Jay, cluster): port-forwards + `python -m evaluation.eval_feedback --reps 3`
   (~30 min). Opcional: limpiar antes los ~30 docs HighCPU del horno nocturno de la `incidents` de prod.
3. **Rellenar resultados** en `docs/10` (el hueco ya está) — salga positivo o null. Si null: anotar
   "el 1.5b no explota el conocimiento negativo → modelo superior o guard determinista en el motor".
4. **Slide de R4** para el deck v2 (`build_demo.py`): Capa A (0%→X%) + Capa B (tabla de 3 arms).
   Declarar el fixture como sintético en la caption.
5. **Correr los tests**: `cd agent && pytest tests/test_eval_feedback.py -v`.
6. **Commit pendiente de Jay** (sin `Co-Authored-By`): harness + fixture + tests + `docs/10`.
   Sugerencia de mensaje: `feat(eval): R4 feedback-loop harness (2 capas) + protocolo pre-registrado`.
7. **Arrastrado de la sesión anterior**: deck v2 sobre `build_demo.py` (working tree ya tocado);
   matriz E1–E6 (`docs/14`); Gate 8 screenshots; F-11/F-17/F-06.
