---
fecha: 2026-07-02
slug: v2-eje-a-enrichment-grounding
promoted: false
---

## Objetivo
Arrancar y cerrar el **Eje A (keystone) de v2 — grounding / P0·1**: el único P0 abierto de la
auditoría. Etapa determinista de *context gathering* entre la cola y el LLM para que el
`current_value` y el **target** de la remediación salgan del **cluster**, no del modelo (el 1.5b
los alucina → `NotFound`/`unparseable` en el slice 6). Tesis: *"el cluster informa, el modelo
razona, el motor dispone"*. Troceado en 1a (snapshot del pod) · 1b (identidad del workload) ·
1c (sellado en el motor).

## Hecho
- **`agent/enrichment.py` (nuevo)** — etapa de grounding fail-soft (nunca bloquea la ingesta;
  fallo/disabled → snapshot parcial → pipeline LLM-only):
  - **1a** — `IncidentSnapshot` (dataclass): `container`, `limits` por-container, `phase`,
    `restart_count`, `last_state_reason` (OOMKilled…), método `current_limit(container, resource)`.
    `_kubectl_json(*args)` (argv, sin shell, timeout corto, **nunca raisea**: rc≠0/timeout/JSON
    inválido/excepción → `None`). `gather_incident_context(labels)` (1 `get pod -o json`) +
    **selección determinista de container** (label → OOMKilled/waiting → único → primero).
  - **1b** — resolución de identidad vía `ownerReferences`: campos `workload_kind`/`workload_name`/
    `match_labels`; `_controller_owner(refs)` + `_resolve_workload()` con cadena canónica
    **pod → ReplicaSet → Deployment** (o STS/DaemonSet directo). El `get` final del controller es
    la **gate de existencia**: `workload_name` solo se fija si el controller existe; `match_labels`
    del `.spec.selector` real.
- **`agent/remediation.py`** — **1c** `seal_proposed_action(diagnosis, snapshot)`: sobreescribe
  `name`/`namespace`/`container`/`current_value` del `proposed_action` con la verdad del cluster.
  3 ramas: sin snapshot/gather fallido/disabled → deja valores LLM (retrocompat); gathered pero
  workload sin resolver → **anula `proposed_action`** (escala, no auto sobre target fantasma);
  gathered+confirmado → sella identidad+`current_value` (LLM conserva `field`+`new_value`).
- **`agent/main.py`** — `gather_incident_context(alert.labels)` tras el RAG + `seal_proposed_action`
  antes de `process_remediation`, en try/except (enrichment nunca tumba el pipeline).
- **`agent/config.py`** — `enrichment_enabled=True` (kill-switch) + `enrichment_timeout=10`.
- **Tests**: `test_enrichment.py` (nuevo, 24: helper·fail-soft·parseo·container·ownerReferences·
  `_controller_owner`) + `TestSealProposedAction` en `test_remediation.py` (+7). Suite 474 →
  **~505**. Verificado por Jay: `test_remediation.py`+`test_enrichment.py` = **205 passed**.

## Encontrado / gotchas
- **Bug real cazado por los tests (1a)**: usé `extra={"args": ...}` en `logger`; `args` es
  atributo reservado de `LogRecord` → `KeyError: "Attempt to overwrite 'args'"`. Habría reventado
  en runtime en **todas** las ramas fail-soft de `_kubectl_json`. Renombrado a `cmd_args`.
  Lección: los otros módulos usan `cmd`/`command`/`error` en `extra` justo por esto.
- **`capture_pre_patch_value` (remediation.py:669) YA leía el valor del cluster** en modo real,
  pero con `name = proposed_action.name` (LLM) → si el nombre del workload está alucinado, el
  `get deployment` falla. Confirmó que el fix no es "leer el valor" sino **sellar la IDENTIDAD**
  antes de que el LLM la toque.
- **`ownerReferences` en vez de strip-hash**: el nombre del RS es `<dep>-<hash>` pero derivarlo por
  regex es la heurística frágil que criticaba la review. La cadena de owners es determinista.
- **Warning preexistente y benigno** (`test_real_execution_multiple_commands`, no tocado):
  `coroutine '_execute_mock_call' was never awaited` — artefacto `MagicMock`+`AsyncMock`
  (`side_effect=[proc_ok, proc_fail]` con procs `MagicMock`), NO bug de producto. Precedente ya
  silenciado en `test_rollback`. Fix opcional: `@pytest.mark.filterwarnings("ignore::RuntimeWarning")`.

## Decisiones + por qué
- **Sellado como paso explícito entre diagnóstico y motor, no dentro de `process_remediation`**:
  blast-radius mínimo (firma y 175 tests del motor intactos), función pura y testeable. El sellado
  es *policy* de remediación ("confía en el cluster sobre el LLM") → vive en `remediation.py`;
  `enrichment.py` queda como capa pura de hechos del cluster (buen layering, sin ciclo de imports).
- **El `get` final = gate de existencia** (acopla "conozco el target" con "existe"): si el
  Deployment no existe, `workload_name=None` → sellado anula `proposed_action` → escala. Cierra la
  clase `NotFound` del slice 6 sin inventar nombres.
- **El LLM queda reducido a `field` + dirección (`new_value`, ya capada ≤2× por regla 4.6)**; los
  *hechos* (identidad + valor actual) del cluster. Es la tesis v2 hecha código.
- **Enrichment fail-soft y retrocompat**: sin cluster (dry-run/tests) `gather_ok=False` → sellado
  deja los valores del LLM → los 175 tests del motor no se rompen. No forcé un flag nuevo más allá
  del kill-switch.
- **Troceado 1a/1b/1c** (microtasks ~20 min) en vez de un módulo monolítico: cada slice compila,
  testea y cierra por sí sola. "Construir despacio y bien".

## Continuación (misma sesión) — selector real + snapshot al prompt

Cerrados los dos primeros ítems del "Siguiente" de arriba. El arco A queda **completo en
código** (1a snapshot · 1b identidad · 1c sellado · **selector real** · **snapshot al prompt**).

### Hecho (2)
- **Fix selector `check_pod_health`** (finding P1 auditoría — rollbacks falsos): el health-check
  post-patch usaba `selector = f"app={name}"` (un guess); si el Deployment no expone `app=<name>`,
  `kubectl get pods -l app=<name>` → 0 pods → "no sanos" → rollback falso de una remediación que
  funcionó. Cableado sin cambiar firmas, reusando el canal `proposed_action`:
  - `remediation.py` `seal_proposed_action`: añade `pa["match_labels"] = snapshot.match_labels`
    (solo si el workload está confirmado).
  - `remediation.py` `capture_pre_patch_value`: `selector` derivado de `match_labels`
    (`",".join(f"{k}={v}")`), con **fallback** a `app={name}` cuando no hay enrichment
    (dry-run/legacy) → retrocompat, los 175 tests del motor intactos.
  - Tests: +2 en `TestCapturePrePatchValue` (selector real multi-label vs. fallback) + 2 en
    `TestSealProposedAction` (propaga labels / clave ausente sin labels). `test_remediation.py` = 185 passed.
- **Snapshot al prompt de `generate_diagnosis`** (Opción A — "el modelo razona"): el snapshot ya
  capturaba hechos deterministas (`last_state_reason`, `restart_count`, `limits`) pero solo se
  usaban para sellar. Ahora alimentan el diagnóstico:
  - `diagnosis.py`: sección `{cluster_context}` en `DIAGNOSIS_PROMPT` ("CLUSTER FACTS — observed,
    authoritative, trust over your own guesses"); helper `format_cluster_facts(snapshot)`
    (fail-soft: `""` si no hay snapshot usable o no hay hechos → retrocompat total);
    `generate_diagnosis(..., snapshot=None)` param opcional.
  - `main.py`: `gather_incident_context` se ejecuta **una vez, antes** del diagnóstico; el mismo
    `snapshot` alimenta el prompt y luego `seal_proposed_action`. **Eliminado el `kubectl get pod`
    duplicado** (antes el gather vivía en el bloque de sellado, post-diagnóstico).
  - Tests: `TestFormatClusterFacts` (5, incluye `restart_count=0` = hecho real, no ausencia) + 2 de
    integración (snapshot entra en el prompt / sin snapshot el prompt queda limpio).
  - Suite `test_diagnosis+remediation+enrichment` = **233 passed** (solo el warning benigno preexistente).

### Decisiones + por qué (2)
- **Threadear `match_labels` por `proposed_action`, no por firma**: `seal` ya escribe verdad del
  cluster ahí y `capture_pre_patch_value` ya lee de ahí → blast-radius mínimo, sin tocar los 2
  call-sites en `process_remediation`. El fallback `app={name}` es la clave de la retrocompat.
- **Gather una vez, arriba, y reusar**: mover el gather antes del diagnóstico habilita el prompt
  injection **y** mata la query redundante (antes: un `get pod` para el prompt sería un 2º get). Un
  solo snapshot sirve a los dos consumidores (prompt + seal). El `seal` no cambia.
- **`format_cluster_facts` duck-typed (getattr) y fail-soft**: espeja a `seal_proposed_action`;
  sin `gather_ok` o sin hechos → `""` → el prompt degrada a la forma alert+RAG previa. Cero regresión
  en los tests de diagnosis existentes.
- **`snapshot=None` como default**: retrocompat de firma; ningún test de `main`/webhook parchea
  `gather`/`seal`, así que el reorden es seguro.

## Continuación (misma sesión) — confidence determinista (gating grounded)

Tercera microtask. La **regla 6** de `decide_action` gateaba el auto con la autoevaluación del
1.5b (sobreconfiada, backlog E5). Ahora la `confidence` que entra al gate viene del cluster para
la clase crash/OOM — "el motor dispone" también en la decisión, no solo en la ejecución.

### Hecho (3)
- **`config.py`**: nuevo gate `remediation_auto_min_restarts: int = 1`.
- **`remediation.py`**:
  - `derive_confidence(snapshot)` (pura): `_FAILURE_REASONS` (OOMKilled/Error/ContainerCannotRun/
    CrashLoopBackOff/DeadlineExceeded) **+** `restart_count ≥ min_restarts` → `1.0`; fallo con menos
    restarts → `0.5`; sin señal de crash (CPU throttle / pod sano / `Completed`) o sin snapshot
    usable → `None` (no override).
  - `ground_confidence(diagnosis, snapshot)`: si hay señal, sobrescribe `diagnosis["confidence"]` y
    preserva la del modelo en `diagnosis["model_confidence"]` (auditoría). **`decide_action` intacto**
    — cambia el *input* de la regla 6, no la regla.
- **`main.py`**: `ground_confidence` justo tras `seal_proposed_action`, mismo try/except fail-soft y
  mismo snapshot único.
- **Tests**: `TestDeriveConfidence` (7: ramas + respeto de `min_restarts`) + `TestGroundConfidence`
  (5: override/preserva model_confidence, no-op sin señal, no-dict, y 2 de interacción con
  `decide_action` — OOM aislado `restart_count=0` → grounded `0.5` → SUGGEST_ONLY; OOM recurrente →
  grounded `1.0` → AUTO_REMEDIATE aunque el modelo estuviera infra/sobre-confiado).
  `test_remediation+diagnosis+enrichment` = **245 passed** (solo el warning benigno preexistente).

### Decisiones + por qué (3)
- **Override solo en clase crash/OOM (no siempre)**: `restart_count` es la señal de *esa* clase; el
  throttling de CPU no reinicia el pod (`last_state_reason=None`). Gatear todo el auto por restarts
  rompería el path CPU (ya flag-gated + ≤2× + estructurado). Cuando no hay señal de crash →
  `None` → queda la confidence del modelo → cero overreach. Fiel a la nota de la review (era OOM).
- **Override en seal-time, gate sin tocar** (vs. regla nueva en la cascada): reusa el canal ya
  montado (`main.py` seal→ground→process), deja el gating en un solo sitio, no threadea el snapshot
  hasta `decide_action`. Un solo punto de verdad para "confía en el cluster sobre el LLM".
- **`_FAILURE_REASONS` como allow-list explícita** (excluye `Completed`): un pod que terminó OK no es
  un incidente; sin la lista, cualquier `last_state_reason` truthy contaría como fallo.
- **Preservar `model_confidence`**: no perdemos la autoevaluación del modelo (auditoría / futura
  calibración), pero el log del incidente (remediation.py:988) persiste ya la grounded, que es la que
  gobierna la decisión.

## Siguiente
- **Cluster (pendiente real)**: build/imagen del arco A + validar en cluster (chaos OOM → el auto
  dispara con target/valor/selector sellados **y** confidence grounded a 1.0 por los restarts, sin
  `NotFound` ni rollback falso); commitear también temp=0 + enrich OOM + Eje B.
- **(Opcional) Exponer `model_confidence` vs `confidence` en el mensaje de Mattermost / métrica**
  para hacer visible el grounding (útil para la presentación al chapter).
- **`/promote`** cuando se consolide: arco A a `07-roadmap` (Eje A ✅; modos de fallo "target
  alucinado", "rollback falso por selector" y "gating por autoevaluación del 1.5b" resueltos;
  snapshot→prompt = "el modelo razona", confidence grounded = "el motor dispone"), `CLAUDE.md`
  (nuevo `enrichment.py` + notas `diagnosis.py`/`remediation.py` + gate `remediation_auto_min_restarts`),
  `11-quality-backlog` (cerrar P0·1 y P1).
