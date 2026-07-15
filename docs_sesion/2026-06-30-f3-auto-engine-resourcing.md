---
fecha: 2026-06-30
slug: f3-auto-engine-resourcing
promoted: true
---

> Quinta sesión del 2026-06-30, continuación directa de `v2-council-auto-remediacion`. Implementa el plan de 6 slices para que la **auto-remediación dispare de verdad** vía re-sourcing de decisiones al motor. Hechos Slices 1, 5, 2/3 y **4** (código + tests). Pendiente: Slice 6 (validación en cluster con chaos). Código y tests yo; pytest y git Jay.

## Objetivo
- Cerrar las 2 desconexiones del motor halladas en la sesión council para que el auto **dispare honestamente**: el motor sintetiza el comando desde `proposed_action` (no ejecuta los investigativos del modelo) y **re-sourcea la decisión de riesgo** (el bound determinista del motor sustituye al `risk` float del modelo).
- Aclaración del usuario a media sesión: **NO ir a la demo sin chaos experiments Y con auto-remediación real** (no solo tarjeta Mattermost que no hace nada). → reencuadre: el rediseño hace el auto alcanzable; F4 ("auto no alcanzable con qwen2.5:1.5b") era sobre el **motor viejo**, no un límite del modelo.

## Hecho
**`agent/remediation.py` (3 cambios de código):**
- **Slice 1 — `build_set_resources_command(deployment, namespace, container, field, value)`**: constructor puro y determinista del `kubectl set resources`. `revert_patch` ahora delega en él. Output **byte-idéntico** al inline anterior (verificado) → refactor sin cambio de comportamiento.
- **Slice 2/3 — re-sourcing**:
  - Nueva `is_structured_remediation(diagnosis) -> bool` (fuente única de verdad): field elegible (memory siempre; cpu iff `remediation_auto_cpu_enabled`) + `name/ns/container` presentes + `current/new` parseables + `current>0` + **solo-subir** (`new>current`). NO comprueba ≤2× (eso lo dueña la regla 4.6).
  - `_set_resources_exception` reescrita → `reason_code==set_resources_triggers_rollout AND is_structured_remediation`. Quitados sus umbrales internos conf≥0.9/risk≤medium.
  - **Regla 5 (riesgo): bypass si `is_structured_remediation`** (+ log de observabilidad). El `risk` del modelo deja de cortar la vía estructurada.
  - **Regla 6 (confianza): SIN cambios** — es el suelo `conf≥0.8` (configurable) para todos, structured incluido.
  - `process_remediation`: si AUTO + structured → sintetiza con `build_set_resources_command(new_value)` y ejecuta **solo ese** comando; si no → `safe_cmds` legacy (compat). Snapshot + rollback ya field-aware.
  - Borradas las constantes muertas `_SET_RESOURCES_EXCEPTION_MIN_CONFIDENCE/MAX_RISK` (sin referencias colgando, verificado).

**Slice 4 — guardrail namespace allow-list (`agent/config.py` + `remediation.py`):**
- Nuevo setting `remediation_auto_namespace_prefix: str = "arturo-"` (default seguro; `""` = sin restricción).
- `is_structured_remediation` rechaza `proposed_action` cuyo `namespace` no empieza por el prefijo → **no auto cross-tenant** en cluster compartido (blast-radius). Secure-by-default.
- `k8s/deployment-agent.yaml`: env explícito `REMEDIATION_AUTO_NAMESPACE_PREFIX="arturo-"` (declara el guardrail; igual al default).

**Build + deploy (Slice 6, en curso):**
- Commit de la feature (`feat(remediation): re-sourcing...`) → SHA `da7aafb`. Eval-debt F4 en commit aparte (test_evaluation + datasets + ground truth).
- Cloud Build `c7b44ffd` **SUCCESS** → `aiops-agent:da7aafb` pusheada. Manifiesto pinneado a `da7aafb` (línea 25, de `5d5d7c7`).
- Deploy declarativo pendiente: `kubectl apply -f k8s/deployment-agent.yaml -n arturo-llm-test`.

**`agent/tests/test_remediation.py`:**
- Imports: `is_structured_remediation`, `build_set_resources_command`.
- **Fix de fixtures (Slice 4)**: los 2 helpers `_cpu_action` y el `_cpu_diagnosis` usaban `namespace="prod"` → con el guardrail dejaban de ser estructurados y los tests CPU-AUTO escalaban. Cambiados a `arturo-llm-test`. (Las fixtures memoria ya usaban `arturo-llm-test`; las "prod" de la regla 4.6 no dependen de la vía estructurada → indiferentes.)
- +2 asserts de guardrail: `test_foreign_namespace_not_structured_escalates` (integración) + caso namespace ajeno en el unit de eligibility.
- **4 tests invertidos** (contrato nuevo, deliberado): memoria `test_set_resources_high_risk_*` y `*_low_confidence_*` + cpu `test_flag_on_high_risk_*` y `*_low_confidence_*` → ahora **AUTO** (renombrados a `*_auto_via_engine_bound` / `*_confidence_above_base_floor_auto`).
- **Nueva clase `TestStructuredAutoRemediation`** (Slice 5, 6 tests): caso headline real (risk=high, conf=0.84, solo investigativos, `proposed_action` válido → AUTO + comando sintetizado ejecutado, NO los `describe/top`); cpu simétrico con flag; cpu flag-off escala; guardrail solo-subir (bajar → ESCALATE); suelo de confianza (conf=0.7 → SUGGEST_ONLY); unit de `is_structured_remediation`.
- Estado pytest: **pendiente de Jay** (suite previa 168 verde tras Slice 1; estos cambios sin correr aún).

## Encontrado / gotchas
- **El conflicto que obligó a parar y pensar**: el rediseño **invierte 4 tests de seguridad de F3** (no 2 como dije al principio). Memoria y CPU son simétricos, así que el contrato "drop risk / keep conf≥0.8" toca ambos. Surfaceado al usuario antes de tocar — no se invierten tests de seguridad a escondidas.
- **Gate de Cloud Build rojo por deuda de F4, no por esta sesión**: `test_evaluation.py::test_all_payloads_valid_against_schema` aserta `len==10` pero `load_datasets()` hace glob `alerts_*.json` → con los datasets nuevos de F4 (highcpu 3 + highmemory 2) carga 15. Arreglado: total→15 + per-file counts (highcpu/highmemory) + existencia. `test_remediation.py` (175) pasó entero, el re-sourcing está limpio. (El `INFO logging.logWriter` de Cloud Build es ruido de permisos, no la causa.)
- **ImagePullBackOff por usar el build ID como tag**: el primer deploy usó `aiops-agent:a6ad74aa-8b4b-...` (el **build ID** de Cloud Build) en `kubectl set image`. Pero `cloudbuild.yaml` etiqueta con `COMMIT_SHA`, no con el build ID → ese tag no existe en el registry (todos son short SHAs de 7 chars). El pod viejo siguió Running 1/1 → **sin outage**. Lección: el tag de deploy es SIEMPRE el short SHA del commit, nunca el build ID.
- **La imagen ya estaba construida y gate-green antes del fix de tag**: build `a6ad74aa` (SUCCESS 21:05) había pusheado `:4534447`/`:latest` con el código nuevo (build context = working tree, gate verde tras el fix de test_evaluation a las 21:01→21:05). Pero el tag `4534447` no correspondía al código (sin commitear) → se eligió el camino limpio (commit→rebuild→deploy con SHA propio `da7aafb`) por procedencia.
- **`test_evaluation.py` depende de datasets untracked**: staged junto a la feature habría dado un commit inconsistente (test cuenta 15 pero los `alerts_highcpu/highmemory.json` no estaban en el árbol). Separado en commit de eval-debt F4 con sus datasets → cada commit gate-green por sí solo.
- **El blocker real estaba en los umbrales de la excepción 4.5 (conf≥0.9, risk≤medium), NO en las reglas 5/6 base** — porque cuando el modelo emite el `set resources`, el flujo pasa por 4.5. Por eso el fix coherente toca AMBOS sitios (4.5 y regla 5), si no el comportamiento sería incoherente según el modelo emita o no el comando.
- **`process_remediation`/`decide_action` están mockeados** en `test_rollback.py` y `test_endpoints.py` → el cambio de lógica solo afecta a `test_remediation.py`. Verificado por grep.
- **Investigativos clasifican SAFE**: `^kubectl\s+(describe|get|logs|top)\s+` → los comandos del caso real pasan rule 4 (no UNKNOWN→suggest). Si el modelo emitiera un investigativo no reconocido, caería a SUGGEST_ONLY (aceptable).
- **≤2× lo mantiene la regla 4.6, no la eligibility** — para preservar su razón/log específicos (`{resource}_exceeds_2x`) y que `test_*_exceeds_2x` siga escalando por la razón correcta.
- `python` no existe en el entorno (solo `python3`); deps no instaladas aquí → validación por AST + lógica aislada, pytest lo corre Jay.

## Decisiones + por qué
- **Drop risk, keep confidence≥0.8** (confirmado por el usuario). *Por qué*: `risk` es auto-rating de peligro del modelo → lo sustituye el bound determinista (field elegible + solo-subir + ≤2× + reversible con health-check). `confidence` mide si el modelo **acertó el diagnóstico** (deployment/field), cosa que el bound NO protege → se mantiene el suelo. Narrativa honesta: "no tiramos todo el juicio del modelo, solo su auto-rating de peligro, que el motor supera". Es **upgrade de seguridad, no rebaja de gate** (ejecutar el string libre de un 1.5B sería peor).
- **Fuente única `is_structured_remediation`** en vez de lógica dispersa. *Por qué*: la usan 4.5, regla 5 y `process_remediation`; una sola definición evita incoherencias y es testeable como unit.
- **Solo-subir como eligibility (no escalate nuevo)**. *Por qué*: bajar un límite no es un fix de presión; si el modelo lo propone, que caiga a los gates normales (probablemente escala), sin añadir una rama de error.
- **Ejecutar solo el comando sintetizado en la vía estructurada**. *Por qué*: los investigativos del modelo son read-only e inútiles de ejecutar en un auto-fix; el sintetizado es determinista y acotado ("el modelo propone, el motor dispone").
- **Reencuadre del usuario sobre la demo**: auto-remediación **sí** es demostrable; F4 medía el motor viejo. El camino: Fase A (Slices 1-5, sin cluster, determinista) → Fase B (Slice 6 = chaos `cpu`/`oom` con auto real + rollback). *Por qué*: da las dos cosas que el usuario exige (chaos + auto que arregla), honestamente.

## Siguiente
- **Deploy declarativo** (pendiente ahora mismo): `kubectl apply -f k8s/deployment-agent.yaml -n arturo-llm-test && kubectl rollout status deployment/agent -n arturo-llm-test` → readyz OK. (`da7aafb` sí existe en el registry, el ImagePullBackOff no se repite.)
- **Slice 6 — chaos con auto real**: `scripts/chaos.sh oom` (auto memoria sin flag) → ver `--limits=memory` subido + tarjeta + rollback; luego CPU escalate-first → `kubectl set env ... REMEDIATION_AUTO_CPU_ENABLED=true` → `scripts/chaos.sh cpu` (auto-fire real). Verificar en logs `Rule 5 bypassed: structured remediation` + `Rule 4.5 exception`.
- Commit del pin del manifiesto (`chore(k8s): pin agent image a da7aafb`).
- ~~pytest~~ ✅ 460 verde (full suite); `test_remediation.py` 175.
- **Slice 6 — validación en cluster (la demo)**: build+deploy imagen nueva; flip de flags (`remediation_enabled=true`, `remediation_dry_run=false`, `remediation_auto_cpu_enabled=true`) en `arturo-*`; `scripts/chaos.sh cpu`/`oom` → auto-fire real + tarjeta Mattermost + rollback con health-check + Grafana. Es el chaos experiment con auto-remediación que la demo necesita. (Guardrail namespace ya protege blast-radius.)
- **Tweak UX (opcional)**: endurecer el prompt para que el modelo emita el `set resources` exacto cuando hay `proposed_action` con field de límite (hoy `has_set_resources=null` 4/5) → tarjeta lista para aprobar de un click. NO desbloquea el auto (ya lo sintetiza el motor) pero mejora la escalación.
- **Pendientes arrastrados**: `/promote` de F3 (slices 1/1b/2) + F4 + evals + esta sesión (5 bitácoras `promoted: true`); matriz E1–E6 (`docs/14`); Gate 8 screenshots; validar self-heal NOGROUP live.
- **Doc de fallo a promover**: "auto limitado por el modelo" (F4) queda **superado** por el re-sourcing — actualizar el modo de fallo en `docs/07` cuando se promueva.
