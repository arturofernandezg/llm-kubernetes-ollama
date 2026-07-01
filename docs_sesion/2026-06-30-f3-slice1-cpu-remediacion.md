---
fecha: 2026-06-30
slug: f3-slice1-cpu-remediacion
promoted: true
---

> Primera sesión de F3 (remediación de CPU). Arrancada con `/start`. Objetivo: cerrar el hueco visible — HighCPU muere hoy en `confidence=0 / suggest_only`, el único de los 4 modos de fallo (OOM, CrashLoop, BadImage, **CPU**) sin acción de remediación. Código y tests yo; pytest y commits Jay.

## Objetivo
- **F3 slice 1**: enseñar al motor de remediación la dimensión CPU para que HighCPU deje de morir en `suggest_only` y produzca una acción estructurada que **escale** a Mattermost con botones aprobables.

## Hecho
- **`agent/diagnosis.py`** — generalizado `DIAGNOSIS_PROMPT`: `proposed_action.field` admite ahora `resources.limits.memory` (existente) **y** `resources.limits.cpu` (nuevo). Reglas del prompt actualizadas (CPU en millicores/cores, `new_value` ≤ 2× `current_value`, comando `kubectl set resources --limits=cpu=<v>`). La extracción de `proposed_action` (líneas 154-156) ya era field-agnostic → la acción CPU sobrevive sin tocar `generate_diagnosis`.
- **`agent/remediation.py`**:
  - Nuevo `parse_cpu_to_millicores(value)` análogo a `parse_memory_to_bytes` (`"250m"→250`, `"1"→1000`, `"0.5"→500`, cores×1000, `ValueError` en inválido).
  - Tabla `_LIMIT_FIELD_PARSERS` (`resources.limits.memory`→bytes, `resources.limits.cpu`→millicores).
  - **Regla 4.6 (cap ≤2×) refactorizada** de memory-only a **dispatch por `field`**: parser según el campo, misma lógica (faltan valores / current==0 / new>2× / no parseable → ESCALATE). Reason codes ahora por-recurso (`{cpu,memory}_exceeds_2x`, `missing_{...}_value`, `zero_current_{...}`, `unparseable_{...}`).
  - Docstring de `decide_action` actualizado (4.6 cubre `{memory,cpu}`).
- **`agent/tests/test_remediation.py`** — `TestParseCpuToMillicores` (7) + `TestDecideActionCpu` (7): CPU set-resources → ESCALATE (rule 4.5); >2× → ESCALATE (rule 4.6 CPU); 2× exacto y unidad cores → pasan el cap (AUTO_REMEDIATE con comando no-restart); inválido/ausente → ESCALATE; comandos de investigación → SAFE.
- **Verificación**: `python3 -m py_compile` limpio. `pytest tests/test_remediation.py` → tras corregir un fallo (abajo) verde.

## Encontrado / gotchas
- **El grounding RAG ya existía**: `agent/runbooks/highcpu.yaml` ya recomienda subir `resources.limits.cpu` y escalar horizontalmente. El gap NO era el RAG — era que **todo el motor era memory-only** (prompt con `field` hardcodeado a memory; regla 4.6 solo memory; `capture_pre_patch_value`/`revert_patch` solo memory; mensaje de rollback en `main.py:256` dice "memory" hardcodeado).
- **Escalate-first sale "gratis"**: como el comando real de remediación CPU es `kubectl set resources --limits=cpu`, la regla 4.5 ya existente (`set_resources_triggers_rollout` + `_set_resources_memory_exception` devuelve False para field=cpu) lo enruta a ESCALATE sin añadir código. No hay que tocar la 4.5 para conseguir el comportamiento "humano aprueba".
- **Fallo en primer pytest**: línea huérfana `assert result["execution_attempted"] is False` quedó pegada al final del smoke test `test_cpu_investigation_commands_classify_safe` → `NameError: name 'result'`. Borrada. (1 failed → verde).
- **Warning preexistente, ajeno**: `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` en `TestDecideActionTutorRule::test_memory_missing_current_value_escalates` — no introducido por este cambio.

## Decisiones + por qué
- **Mecanismo: bump vertical de CPU limit, NO scale horizontal ni HPA** (decidido con Jay).
  - *Por qué vertical*: refleja el camino de memoria ya probado → reusa snapshot/health/rollback con cambios mínimos (generalizar `field`), cero dependencias nuevas de cluster. "Construir despacio y bien".
  - *Por qué NO scale*: hoy rule 4.5 fuerza ESCALATE para `scale`, y su rollback (escalar de vuelta) tiene semántica distinta a set-resources → más código nuevo, no reusa el snapshot.
  - *Por qué NO HPA*: requiere `metrics-server` (sin confirmar en GKE) y es cambio de estado persistente, no remediación puntual.
- **Comportamiento: escalar primero, auto después** (decidido con Jay). Slice 1 escala (humano aprueba); la auto-remediación de CPU (excepción análoga a 4.5) será un slice posterior ya validado. *Por qué*: el modelo pequeño es overconfident (backlog E5) y no hemos validado el camino CPU en cluster — meter humano en el bucle es lo prudente para el primer corte.
- **Generalizar la 4.6 a CPU aunque en escalate-first no cambie el enrutado** (la 4.5 ya escala CPU set-resources). *Por qué*: documenta el límite de seguridad, deja el motor coherente field-aware y prepara el slice 2 (auto) sin deuda. El cap también atrapa propuestas absurdas si el comando fuese no-restart.

## Siguiente
- **Jay**: `cd agent && python3 -m pytest tests/test_remediation.py -q` (confirmar verde; suite total sube de 422, +14 tests — el conteo se actualiza en `/promote`, no a mano). Commits sugeridos (sin `Co-Authored-By`):
  - `feat: motor de remediación aprende dimensión CPU (set resources cpu, escalate-first) [F3]` → `agent/diagnosis.py` + `agent/remediation.py`.
  - `test: cobertura del camino de remediación CPU (parse millicores + decide_action escalate)` → `agent/tests/test_remediation.py`.
- **Slice 1b** (necesario ANTES de validar en cluster — sin él, aprobar una escalación de CPU mal-ejecuta/mal-revierte como memoria):
  - `capture_pre_patch_value`: parametrizar el `jsonpath` por `field` (cpu vs memory).
  - `revert_patch`: construir `--limits=<cpu|memory>=value` según `snapshot.field` (hoy hardcodea `memory`, ~línea 698).
  - `agent/main.py:256`: el mensaje de rollback dice "memory" hardcoded → derivar la palabra del recurso de `snapshot.field`.
  - Tests de la generalización capture/revert.
- **Slice 2** (tras validar en cluster): excepción de auto-remediación de CPU (análoga a 4.5) si el chapter lo pide.
- **Cluster (Jay, tras 1b)**: `scripts/chaos.sh cpu` → regla `HighCPU` (rate cpu / limit > 0.9, 5m) → agente → diagnóstico con `proposed_action(cpu)` → ESCALATE con botones (ya no `suggest_only`). Confirma que el camino vertical NO necesita `metrics-server`.
- **Track aparte**: scale horizontal / HPA (HPA bloqueado hasta confirmar `metrics-server`).
- **`/promote`** al consolidar: `docs/02` (motor CPU), `docs/07` (F3 slice 1 hecho + changelog), CLAUDE.md (descripción de `remediation.py`: dimensión CPU + cap ≤2× field-aware), conteo de tests.
- **Pendiente de siempre (cluster, sesiones previas)**: validar live el self-heal NOGROUP (imagen nueva sin commitear/buildear); bump Redis + corte de ingestión; matriz E1–E6 (`docs/14`); screenshots Gate 8 con caudal.
