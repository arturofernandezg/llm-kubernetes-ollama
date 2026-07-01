---
fecha: 2026-06-30
slug: f3-slice1b-motor-field-agnostic
promoted: true
---

> Segunda sesión de F3 del 2026-06-30 (tras `f3-slice1-cpu-remediacion`). Arrancada con `/start` → "siguiente?". Objetivo: quitar el acoplamiento latente a "memory" en el camino AUTO_REMEDIATE para dejar el motor field-agnostic. Código y tests yo; pytest y commits Jay.

## Objetivo
- **F3 slice 1b**: generalizar `capture_pre_patch_value`, `revert_patch` y el mensaje de rollback para que deriven el recurso (`cpu`|`memory`) de `proposed_action.field` / `snapshot.field`, en vez de hardcodear "memory".

## Hecho
- **`agent/remediation.py`**:
  - Nuevo helper `_limit_resource(field)` → `"cpu"` | `"memory"` (default `memory` para campos desconocidos/vacíos/None).
  - `capture_pre_patch_value`: el jsonpath ahora es `{.resources.limits.<resource>}` derivado de `field` (antes fijo a `.memory`). Texto del warning generalizado ("empty memory limit" → "empty limit", con `resource` en extra).
  - `revert_patch`: construye `--limits=<resource>={snapshot.value}` desde `_limit_resource(snapshot.field)` (antes fijo a `memory`).
- **`agent/main.py`**: importa `_limit_resource`; en `_evaluate_rollback` el mensaje "Reverted ... memory to ..." deriva la palabra del recurso de `ctx.snapshot.field`.
- **`agent/tests/test_remediation.py`**:
  - `TestCapturePrePatchValue` +2: dry-run cpu; real-mode cpu que **afirma que el jsonpath enviado a `create_subprocess_exec` contiene `resources.limits.cpu` y NO `resources.limits.memory`** (inspección de `mock.call_args.args`).
  - `TestRevertPatch` +1: snapshot cpu → `result.command` contiene `--limits=cpu=500m`, sin "memory".
  - `TestLimitResource` +5: cpu/memory/unknown→memory/empty→memory/None→memory.
- **Verificación**: `python3 -m py_compile remediation.py main.py tests/test_remediation.py` limpio. pytest lo corre Jay.

## Encontrado / gotchas
- **CORRECCIÓN al log `f3-slice1-cpu-remediacion`**: ahí dije que el slice 1b era *bloqueante* para validar el slice 1 en cluster. **Es falso.** Trazando el código:
  - El handler de aprobación de escalaciones (`agent/main.py:968`, `handle_action_callback` rama `approve`) **sólo** hace `execute_commands(incident.safe_commands)` — **no captura snapshot ni programa rollback**.
  - `_format_escalation_body` (`main.py:573`) renderiza `safe_commands` de forma genérica (sin texto "memory").
  - → El camino **escalate→approve→`set resources cpu`** ya funcionaba end-to-end tras el slice 1, sin tocar nada más.
- **Dónde vivían realmente los hardcodes de "memory"**: SÓLO en el camino **AUTO_REMEDIATE** (`capture_pre_patch_value` jsonpath, `revert_patch` `--limits=memory`, `main.py:256` mensaje). Como CPU es escalate-first, hoy nunca los toca → eran acoplamiento latente, no un bug activo. Por eso 1b es **prerequisito del slice 2 (auto-CPU)**, no del slice 1.
- **El rollback sólo guarda acciones autónomas**: las remediaciones aprobadas por humano NO tienen rollback automático (diseño: el humano vigila lo suyo). Confirmado en `handle_action_callback`.
- Los tests de `test_rollback.py` usan snapshot de memoria y no asertan la palabra "memory" → el cambio de `main.py:256` no los rompe.

## Decisiones + por qué
- **Hacer 1b ahora aunque no sea bloqueante** (elegido por Jay sobre "cerrar F3-escalate en docs" u "otro frente"). *Por qué*: refactor pequeño y de bajo riesgo que elimina el acoplamiento latente a memoria y deja el motor honesto/field-agnostic; desbloquea el slice 2 sin deuda. "Construir despacio y bien".
- **Helper `_limit_resource` con default `memory`** en vez de fallar ante un field desconocido. *Por qué*: preserva el comportamiento histórico (snapshots viejos o field vacío siguen tratándose como memoria) y evita romper el camino AUTO de memoria ya probado. Fail-safe hacia el caso conocido.
- **Test que inspecciona `mock.call_args` del jsonpath** en vez de sólo el valor parseado. *Por qué*: el valor "500m" se parsearía igual aunque el jsonpath siguiera pidiendo memory; sólo afirmando el jsonpath se prueba de verdad que consultamos el recurso correcto.

## Siguiente
- **Jay**: `cd agent && python3 -m pytest tests/test_remediation.py tests/test_rollback.py -q` (verde, +8 tests sobre slice 1). Commit sugerido (sin `Co-Authored-By`):
  - `refactor: capture_pre_patch_value/revert_patch derivan recurso de field (cpu/memory) [F3]` → `agent/remediation.py` + `agent/main.py` + `agent/tests/test_remediation.py`.
- **Slice 2** (tras validar en cluster): excepción de auto-remediación de CPU (análoga a la 4.5) que enrute `set resources cpu` con conf≥0.9/risk≤medium a AUTO_REMEDIATE. Es lo que *usa* la generalización de 1b (capture/revert/mensaje en cpu).
- **Validación en cluster del slice 1** (Jay; ya NO bloqueada por 1b): `scripts/chaos.sh cpu` → `HighCPU` → agente → diagnóstico `proposed_action(cpu)` → ESCALATE con botones → aprobar → `set resources cpu`. Necesita imagen nueva (que además arrastra el self-heal NOGROUP sin buildear). Confirma que el camino vertical NO necesita `metrics-server`.
- **Track aparte**: scale horizontal / HPA (HPA bloqueado hasta confirmar `metrics-server`).
- **`/promote`** al consolidar (slice 1 + 1b juntos): `docs/02` (motor CPU + capture/revert field-agnostic), `docs/07` (F3 slice 1+1b hechos + changelog), CLAUDE.md (descripción de `remediation.py`: dimensión CPU, cap ≤2× field-aware, capture/revert derivan recurso de field), conteo de tests (422 → +22).
- **Pendiente de siempre (cluster, sesiones previas)**: validar live el self-heal NOGROUP (imagen nueva sin buildear); bump Redis + corte de ingestión; matriz E1–E6 (`docs/14`); screenshots Gate 8 con caudal.
