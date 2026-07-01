---
fecha: 2026-06-30
slug: f3-slice2-auto-cpu
promoted: true
---

> Tercera sesión de F3 del 2026-06-30 (tras `f3-slice1-cpu-remediacion` y `f3-slice1b-motor-field-agnostic`). Arrancada con `/start` → micro-objetivo "slice 2 (auto-CPU)". Código y tests yo; pytest, git y builds Jay. Incluye además limpieza de git/provenance e **intento de validación en cluster** (con hallazgo importante).

## Objetivo
- **F3 slice 2**: excepción de auto-remediación de CPU análoga a la regla 4.5 de memoria, de modo que `set resources cpu` de alta confianza/riesgo acotado pueda auto-remediarse en vez de escalar. **Decidido detrás de un flag** (`remediation_auto_cpu_enabled`, default off) para honrar la secuencia de slice 1 ("escalar primero, auto tras validar en cluster").

## Hecho
- **`agent/config.py`**: nueva setting `remediation_auto_cpu_enabled: bool = False`.
- **`agent/remediation.py`**: `_set_resources_memory_exception` → renombrada a `_set_resources_exception` y generalizada: memory siempre elegible (tutor-approved); cpu elegible **solo si `settings.remediation_auto_cpu_enabled`** (si no, `return False` → escala); cualquier otro field → `False`. Mismos umbrales conf≥0.9/risk≤medium reutilizando las constantes. Caller (regla 4.5 en `decide_action`) con log `"Rule 4.5 exception: authorized set-resources change"` + `resource` derivado de `_limit_resource(field)`. Comentario de constante y docstring actualizados.
- **`agent/tests/test_remediation.py`**: nueva clase `TestDecideActionCpuAuto` (+6 tests flag-on: AUTO central, flag-off→ESCALATE, conf<0.9→ESCALATE, risk=high→ESCALATE, >2×→ESCALATE por 4.6, scale-guard). Docstrings de no-regresión aclarados a "flag off, default"; `TestDecideActionCpu._base_monkeypatch` fija el flag a False explícito. **165 passed** (pytest local de Jay).
- **`k8s/deployment-agent.yaml`**: env `REMEDIATION_AUTO_CPU_ENABLED=false` con comentario (flip de un solo edit tras validar).
- **Limpieza git/provenance** (3 commits): `a8993dc` feat F3 (slice 1+1b+2: config/diagnosis/remediation/test_remediation/deployment-agent); `830e5d2` feat(F2) self-heal NOGROUP (main/streams/test_streams, que llevaban tiempo sin commitear); `4534447` docs(F2) promoción cola + **destrackeo de `agent/.pytest_cache/`**.
- **Build + deploy**: Cloud Build (gate de tests OK) → imagen `aiops-agent:4534447` → `kubectl set image` + rollout OK en `arturo-llm-test`.
- **Validación en cluster** (flag off): HighCPU recurrente sobre el propio agente → pipeline completo (RAG 3 runbooks/2 incidents → diagnóstico → `outcome=escalate` → incidente en ChromaDB → escalación en Redis → tarjeta en Mattermost con botones). `port-forward svc/mattermost-svc 8065`.

## Encontrado / gotchas
- **HALLAZGO PRINCIPAL — el modelo no dispara el camino CPU**: las tarjetas de Mattermost de las escalaciones HighCPU muestran SOLO comandos de investigación (`describe pod`, `top pod`, `logs --previous`) — **ningún `set resources --limits=cpu=`**. El `qwen2.5:1.5b` no genera `proposed_action(cpu)`; produce read-only + `risk=high`/`conf=0.85`. → Escala por la **regla 5 (risk high)**, NO por las reglas 4.5/4.6 de CPU. Consecuencia: el chaos en vivo **no demuestra** ni slice 1 (escalación de set-resources cpu) ni slice 2 (auto), porque el input estructurado nunca llega al motor. El motor es correcto y está testeado; el modelo pequeño simplemente no propone la acción. "Docs reflect reality": F3 entrega el mecanismo, no una demo E2E dependiente del modelo.
- **Build mis-tagging (provenance)**: `gcloud builds submit .` empaqueta el **working dir** (incluye cambios sin commitear), pero `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)` solo pone el **tag**. Al buildear sin commitear, salió `aiops-agent:fff2591` conteniendo código de slice 2 → tag colisiona con el commit real `fff2591` (fix Redis) → trazabilidad rota. Fix: commitear primero, re-buildear con árbol limpio (→ `:4534447`).
- **`.pytest_cache` trackeado pese a `.gitignore`**: el `.gitignore` ya tenía `.pytest_cache/` (línea 23), pero los ficheros estaban trackeados de antes y gitignore no excluye lo ya trackeado. Fix: `git rm -r --cached agent/.pytest_cache` (gitignore evita que vuelva).
- **F2 estaba sin commitear**: el self-heal NOGROUP (`streams.py`/`main.py`) y la promoción de docs llevaban tiempo en el working tree (las bitácoras F2 lo marcaban como "imagen sin commitear"). Cerrado hoy en `830e5d2`/`4534447`.
- **El agente (y antes redis) autodisparan HighCPU**: alertas reales por tocar el límite de CPU (agente 300m) en los picos de inferencia (~200-270s por diagnóstico), autorresueltas. No es el foco, pero es un hallazgo ops: o subir el límite del agente, o afinar la regla HighCPU.
- `git add .` mezcló F2 + F3 + cache; se desenredó con `git reset` + adds selectivos.

## Decisiones + por qué
- **Auto-CPU detrás de flag (default off)** en vez de simétrico con memoria. *Por qué*: ninguna imagen contenía aún slice 1/1b/2 → la próxima los lleva juntos; sin flag, el deploy saltaría directo a auto-CPU sin pasar por el escalón "validar escalate-first" que slice 1 diseñó. El flag deja aterrizar el código + tests verdes, pero mantiene escalate-first en cluster hasta `REMEDIATION_AUTO_CPU_ENABLED=true`. Cero churn en los tests que afirman CPU→ESCALATE (siguen siendo el default). Memoria intacta (sin flag).
- **Commitear F2 por separado de F3 y limpiar el árbol antes de re-buildear**. *Por qué*: la imagen debe corresponder 1:1 al código commiteado (era justo el problema del mis-tag). Commits atómicos por unidad lógica (F3 / F2-código / F2-docs+cache).
- **Validar slice 2 con test de integración mockeado, no en vivo**. *Por qué*: el modelo no emite `proposed_action(cpu)`, así que forzar el camino auto en cluster requeriría bajar umbrales (inseguro/deshonesto). Un test con diagnóstico mockeado valida el motor de forma determinista y deja la conclusión honesta: el gate funciona; la activación real depende de un modelo que clasifique el bump de CPU como bajo riesgo, cosa que `qwen2.5:1.5b` no hace.

## Siguiente
- **Test de integración del camino auto-CPU** (yo, ofrecido y pendiente): mock `proposed_action(field=cpu, 250m→500m)` + comando real `set resources cpu` + `conf=0.9`/`risk=low` → flag-off `ESCALATE` / flag-on `AUTO_REMEDIATE` → `set resources cpu` + rollback CPU (capture/revert field-aware de slice 1b). Sin cluster ni LLM.
- **Decidir sobre el auto-alert del agente**: subir `limits.cpu` del deployment agente (hoy 300m) o afinar el `for:`/umbral de la regla HighCPU para no auto-alertarse en cada inferencia. Apuntarlo como finding ops.
- **(Track aparte, F4-ish)** Mejorar runbook/prompt de HighCPU para empujar al modelo a proponer `set resources cpu` — incierto y dependiente del modelo; no bloquea F3.
- **`/promote`** al consolidar F3 (slice 1+1b+2 juntos): `docs/02` (motor CPU + flag auto + capture/revert field-agnostic), `docs/07` (F3 slice 1+1b+2 hechos + changelog + el hallazgo "el modelo no propone CPU"), CLAUDE.md (`remediation.py`: dimensión CPU, flag `remediation_auto_cpu_enabled`, cap ≤2× field-aware), conteo de tests, e imagen actual `4534447`. Incluir el hallazgo de cluster como modo de fallo / nota de honestidad.
- **Pendiente de siempre**: matriz E1–E6 (`docs/14`); screenshots Gate 8 con caudal; validar el self-heal NOGROUP live (ya horneado en `4534447`).
