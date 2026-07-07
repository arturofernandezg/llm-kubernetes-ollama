---
fecha: 2026-07-06
slug: r2-human-fix-cured-validado
promoted: true
---

## Objetivo
Cerrar el **gap R2-humano** encontrado en el run del 07-04 (el `cured` de un approve humano no
entraba al bucle de aprendizaje) y, con el fix desplegado, **re-run del arco `cured` en cluster**
para sacar el `aiops_feedback_verdict_total{outcome="cured"}` real → cerrar S3·b y habilitar R4.

## Hecho
- **Fix del gap R2-humano** (`agent/main.py`, commit `ca159be`) — NO eran "2 líneas" (ver gotchas):
  - `PendingEscalation` + campo `incident_doc_id: str = ""` (el doc_id de ChromaDB calculado en el
    pipeline viaja con la escalación).
  - `_escalation_to_dict`/`_dict_to_escalation`: round-trip del campo por Redis, con
    `.get(..., "")` para escalaciones pre-fix (back-compat).
  - Creación de la escalación: puebla `incident_doc_id=incident_doc_id or ""`.
  - Rama approve: `remediation_for_feedback` se construye ANTES del schedule; pasa
    `doc_id=incident.incident_doc_id` + `remediation=remediation_for_feedback` a
    `_schedule_rollback_evaluation` (espejo exacto del camino auto de `_process_alert_with_diagnosis`).
  - Ingest final del approve: **reusa el mismo doc_id** y marca `outcome=auto_pending` si hay
    rollback programado → una sola doc provisional→veredicto, sin duplicado que contamine retrieval.
- **Test de paridad** `test_approve_feeds_verdict_loop_with_doc_id` en `TestApproveStructuredParity`
  (test_endpoints.py): schedule recibe doc_id+remediation; ingest reusa doc con `auto_pending`.
  **Tests: 620 → 621, todo verde** (pytest global de Jay).
- **Build + deploy**: Cloud Build SUCCESS → imagen **`aiops-agent:0914611`** desplegada
  (`kubectl set image deployment/agent` + rollout OK). `k8s/deployment-agent.yaml` actualizado a
  `0914611` en working tree (commit pendiente, sin rebuild — es commit solo-manifiesto).
- **Run `cured` en cluster — EL PAYOFF, S3·b CERRADO**:
  - Pre-vuelo: imagen `0914611`, sin cooldown/rollback residual; 4 escalaciones huérfanas del 07-04
    borradas de Redis.
  - Pod `54f6d67bfb-rgxsp` OOMKilled puro a 32Mi (restarts=6, `lastReason=OOMKilled`).
  - Pipeline: `snapshot gathered` → `seal current_value=32Mi` → `grounded=1.0 model=0.9` →
    `memory_exceeds_2x` (512Mi=16×) → escalate → **approve humano** → patch OK → +300s →
    `healthy` → **`aiops_feedback_verdict_total{outcome="cured"} 1.0`** +
    `aiops_remediation_rollback_total{healthy}=1.0`. El counter incrementa DESPUÉS del
    `ingest_incident` → el doc en ChromaDB quedó marcado `cured` (las dos señales de una).
  - Teardown ejecutado como último gate del run (delete deployment + limpieza escalaciones Redis).
- Screenshot MM del mensaje de éxito `cured` (Gate 8, el positivo que faltaba) — Jay.

## Encontrado / gotchas
- **El "fix de 2 líneas" no lo era**: `make_incident_doc_id()` embebe `int(time.time())` → el doc_id
  NO es reproducible en el approve (ocurre en otro request, minutos después). Hay que **acarrearlo**
  por la escalación en Redis, no re-derivarlo. El auto lo tiene fácil porque calcula doc_id y
  programa rollback en el mismo request. Moraleja: paridad humano/auto exige que TODO el contexto
  del incidente viaje con la escalación.
- **Mismatch de tag build/manifiesto**: el commit del bump del manifiesto (`da7aafb→ca159be`) movió
  HEAD → el build con `COMMIT_SHA=$(git rev-parse --short HEAD)` salió como **`0914611`**, no
  `ca159be`. Bucle potencial: cada commit de manifiesto invalida su propio tag. Salida: NO
  re-commitear+rebuildear; `0914611` es hijo de `ca159be` solo-manifiesto → mismo código → se
  despliega `0914611` y el commit del manifiesto (ya corregido a `0914611`) se hace al cierre sin rebuild.
- **EL HORNO NOCTURNO** (la gran lección operacional): el target "curado" del 07-04 quedó vivo a
  512Mi con `stress` infinito → propiedad no anticipada del fix "sin `--timeout`": un cured **ya no
  OOMea pero quema CPU para siempre**. ~36h de horno → HighCPU flapping toda la noche (2:34–10:33
  visibles), ~30 diagnósticos LLM molidos, docs basura en ChromaDB, escalaciones free-text apiladas.
  El cleanup era el punto 4 del "Siguiente" de la bitácora anterior y no se ejecutó → **el teardown
  no puede depender de la memoria humana; es el último gate del run, no una tarea posterior**.
- **Forbidden en `kubectl top` (aprobado por error una escalación HighCPU nocturna)**: HighCPU va
  por el camino free-text (auto-CPU tras flag, off) → LLM propone `top pod`/`top node` → validation
  layer los clasifica SAFE (read-only, correcto) → al ejecutar: `Forbidden` (SA sin `metrics.k8s.io`;
  `top node` además es cluster-scoped). **No es bug de RBAC, es least-privilege funcionando** y
  fail-honest (`[FAILED exit=1]` visible). El gap real: la validation layer valida *seguridad* pero
  no *factibilidad* (¿tengo permiso?).
- **Carrera de alertas benigna**: la escalación buena entró por `KubePodCrashLoopBackOff` (ganó a
  `KubePodOOMKilled` — mismo pod, restarts>3). El grounding la selló idéntico (`32Mi`, `grounded=1.0`,
  cap 4.6). El arco es robusto a cuál de las dos alertas gane la carrera — material deck.
- Las alertas "llegaron de golpe por la mañana" = ilusión de UI: el agente postea a MM in-cluster
  toda la noche (no necesita el port-forward de Jay); se ven al abrir el navegador. El websocket de
  MM sobre port-forward sigue flaky (refrescar 2×) — cosmético, ya conocido del 07-04.

## Decisiones + por qué
- **Acarrear el doc_id en `PendingEscalation` (no re-derivarlo)**: el doc_id lleva timestamp → única
  forma correcta de que el veredicto humano escriba el MISMO doc. Alternativa descartada: doc_id
  determinista sin timestamp (rompería el histórico de incidentes repetidos del mismo alertname).
- **Ingest del approve reusa doc_id + `auto_pending`**: sin esto el approve creaba una **doc nueva**
  (segundo timestamp) y quedaban dos: la del pipeline y la del approve — el veredicto solo
  actualizaría una. Una doc por incidente = invariante del bucle R2.
- **No rebuildear tras el mismatch de tag**: `0914611` = `ca159be` + 1 línea de manifiesto → mismo
  código. Rebuildear "para que cuadre el tag" habría metido otro ciclo build/deploy sin valor y el
  mismo problema en el siguiente commit. El tag de deploy es el short SHA que BUILDEÓ, no el último commit.
- **No ampliar RBAC para `kubectl top`**: dar `metrics.k8s.io` (y un ClusterRole para nodes) solo
  para que el camino free-text no falle viola least-privilege y la convención de no ClusterRoles.
  El fallo transparente es mejor historia que el permiso silencioso. En su lugar → backlog:
  **pre-flight `kubectl auth can-i`** al sellar comandos free-text; los no ejecutables se muestran
  como "comando sugerido (el agente no tiene permisos)" en vez de ejecutar-y-fallar.
- **Manifiesto chaos post-run con `--vm-hang 0`** (propuesto, NO aplicado — no se toca un run vivo):
  `stress --vm 1 --vm-bytes 100M --vm-hang 0` asigna y se duerme reteniendo memoria → a 32Mi sigue
  OOMeando (la asignación revienta el límite) pero el estado curado a 512Mi queda residente y
  silencioso (CPU~0) → un cured olvidado ya no incendia HighCPU. A validar en cluster antes de fiarlo.
- **Teardown dentro del run**: tras el veredicto, `delete deployment` + limpieza de escalaciones es
  parte del experimento (consecuencia directa del horno nocturno).

## Siguiente
1. **R4 — métrica feedback-loop gain**: `eval_retrieval` con incidents poblados (ya hay `cured` +
   `rolled_back` reales en ChromaDB — prerequisito cumplido HOY) vs vacío → gráfica para el deck.
2. **Commit pendiente de Jay**: `git add k8s/deployment-agent.yaml docs_sesion/` + commit
   (`chore(k8s): bump deployment-agent a 0914611 (imagen con fix R2 paridad humano/auto)`). Sin rebuild.
3. **Gate 8**: screenshot MM del éxito `cured` guardado (confirmar) + resto de screenshots Grafana.
4. **`/promote`** de esta bitácora: C-02 cerrado y validado en cluster; lección teardown a docs/12;
   pre-flight `auth can-i` a docs/11 (backlog); gotcha mismatch tag build/manifiesto a CLAUDE.md/docs/04;
   `--vm-hang 0` como mejora propuesta del manifiesto chaos.
5. **Pendiente arrastrado**: F-11/F-17; matriz E1–E6 (`docs/14`); S5 deck (3 slides de la review) +
   ensayo; S6 F-06 (historia durabilidad Redis).
6. (Menor) El run dejó ~30 docs HighCPU free-text de la noche en ChromaDB — legítimos como histórico,
   pero si ensucian el eval de R4, considerar filtrarlos/limpiarlos antes de medir.
