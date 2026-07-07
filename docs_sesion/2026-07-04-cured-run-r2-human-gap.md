---
fecha: 2026-07-04
slug: cured-run-r2-human-gap
promoted: true
---

## Objetivo
Sesión de tres tramos encadenados: (1) **fix** del manifiesto chaos que causó el falso-rollback
del 07-04; (2) **`/promote`** de las 4 bitácoras pendientes a docs canónicos + vault; (3) **run
limpio del arco `cured`** en cluster (S3·b) — el veredicto positivo que faltaba para cerrar la
validación v2 y habilitar R4.

## Hecho
- **Fix del falso-rollback** (`k8s/chaos/chaos-oom.yaml`): quitado `--timeout` del stress →
  `args: ["--vm","1","--vm-bytes","100M"]` (stress infinito). Comentario explicando el porqué.
  Física: con `--timeout 60`, al subir la memoria el stress salía exit 0 cada 60s → restart benigno
  que el health-check leía como crash. Sin timeout, un restart solo puede significar OOM.
- **`/promote` completo** (fuente: bitácoras `promoted:false`):
  - `docs/07`: imagen `da7aafb→8a40fdc` desplegada+validada, tests →620, F-03 done, sprint S1–S3 ✅
    + S3·b nuevo, Eje A/F3 validados en cluster, +2 modos de fallo (falso-rollback, LLM techo hardware),
    entrada de changelog.
  - `CLAUDE.md`: estado (imagen, 620, F-03, validación cluster + hallazgos), rag.py F-03, siguiente=S3·b.
  - `docs/06`: recuentos reales por fichero + Total **620** (615 grep, pytest 620); `TestChromaOffloading`,
    R2/R2·3/R3.
  - `docs/11`: F-03 TODO→DONE, F-01 decisión cerrada, sección **C-01..C-05** (hallazgos cluster).
  - `docs/12`: sección "Validación del arco completo v2" (falso-rollback, límite del harness, MTTR=hardware).
  - **Vault**: nodo proyecto `AIOps_Infra_Agent.md` (fecha, 620, estado, 5 patrones reutilizables nuevos)
    + **nodo nuevo** `03_Knowledge/AI_ML/RAG_Feedback_Loops.md` (4 patrones del bucle de aprendizaje).
  - 4 bitácoras marcadas `promoted:true`.
- **Run `cured` en cluster** (S3·b, a mano, NO `scripts/chaos.sh`):
  - Pre-vuelo verde (imagen 8a40fdc, RBAC yes, readyz 200, sin cooldown residual, verdict counter a 0).
  - `kubectl apply` → pod `54f6d67bfb-mwkdq` → **crash-loop OOMKilled puro** (`-w` confirmó: cada restart =
    `Reason: OOMKilled`, cero restarts benignos — el fix funciona).
  - Enrichment `snapshot gathered` (last_state_reason=OOMKilled, workload=Deployment), `ground_confidence
    grounded=1.0 model=0.95`, `Remediation blocked: memory_exceeds_2x` (512Mi) → **escalate** con botones.
  - **Approve HMAC OK** → `capture_pre_patch_value: 32Mi` → `Remediation command succeeded … --limits=memory=512Mi
    exit_code:0` → `deployment … resource requirements updated` → `Rollback evaluation scheduled timeout_s:300`
    + clave durable `rollback:af2a67df…` en Redis (P0·3).
  - **+300s → `Rollback evaluation: pod health check, healthy: true, reason: all_pods_running_no_restarts`**
    → NO revert → MM mensaje de éxito. **El arco `cured` está validado a nivel de comportamiento.**

## Encontrado / gotchas
- **EL HALLAZGO — gap R2 en el approve humano** (causa raíz concreta de C-02): el veredicto `cured` NO
  incrementó `aiops_feedback_verdict_total` ni re-marcó el doc en ChromaDB. Razón: `_reupsert_incident_outcome`
  tiene guarda `if not ctx.doc_id: return` (main.py:231); el `RollbackContext` del approve llega con
  `doc_id=""` porque la llamada de la rama approve **no pasa `doc_id`**:
  - Auto (main.py:956): `_schedule_rollback_evaluation(..., doc_id=incident_doc_id, ...)` ✅
  - Approve (main.py:1234): `_schedule_rollback_evaluation(incident.incident_id, pre_patch_snapshot,
    incident.alert_item, incident.diagnosis)` ❌ sin `doc_id` (ni `remediation`) → default `""`.
  El re-upsert R2 solo estaba cableado para el auto. `aiops_remediation_rollback_total{healthy}` SÍ subió
  (el camino cured se ejecutó), pero la capa de aprendizaje (verdict_total + doc marcado) se saltó en el
  approve humano. Fix de 2 líneas (espejo del auto).
- **"Se remedió solo" = ilusión del texto del LLM**: el diagnóstico free-text del qwen2.5:1.5b redacta la
  acción propuesta en pasado ("increased memory to 512Mi…") → parece que actuó. Pero `action=escalate`,
  `snapshot_captured=false` → NADA ejecutado hasta el botón. El gate real son los botones, no la narración.
  Buen material deck (la confianza/redacción del modelo ≠ que actuó → por eso el humano decide).
- **C-03 en vivo**: aprobar una escalación vieja (pod `7784f95969`, de las 18:13/18:42, >60min) →
  "escalación no encontrada o expirada" (TTL 60min). La escalación fresca (`54f6`, 21:16) sí aprobó.
- **Mattermost UI se desconecta con cada update** (websocket + `kubectl port-forward` flaky): hay que refrescar
  el navegador. **Cosmético** — el click del botón es un POST HTTP (no el websocket), llega al agente aunque el
  live-update esté caído; el agente es server-side, no depende del navegador. Lección: no fiarse de la UI de MM
  para el estado; la verdad está en kubectl/logs.
- **MTTD/MTTR enormes** (1925s, 3785s) porque el pod llevaba mucho rato OOMeando (`startsAt` viejo); no es el
  pipeline. LLM 74-146s por diagnóstico (techo hardware confirmado de nuevo). Alertas re-firing + LLM serial →
  backlog de varias escalaciones apiladas para el mismo workload.
- Múltiples escalaciones huérfanas (pod viejo ya reemplazado): el comando sellado apunta al **Deployment**, no
  al pod, así que aprobar cualquiera parchea igual — pero ensucia el canal.

## Decisiones + por qué
- **Fix del manifiesto sin `--timeout` (no escenario B con 40M)**: se eligió el OOM infinito real en vez de
  bajar el stress para que el auto a 64Mi cure. Motivo: el `--timeout` era la RAÍZ del falso-rollback; quitarlo
  lo mata de raíz y deja el manifiesto correcto para siempre. El escenario B esquivaba el problema, no lo cerraba.
- **Aprobar 512Mi (humano) en vez de forzar el auto**: es la historia narrable (salto >2× que el cap escala a
  humano) y valida la paridad humano/auto E2E. El coste: destapó el gap R2-human (bueno — es justo lo que el
  run existe para encontrar).
- **Cerrar el gap R2-human (opción 1) en vez de esquivarlo por el auto (opción 2)**: la paridad humano/auto es
  la tesis del sistema ("what you approve is what runs"); un `cured` humano que no entra al bucle de aprendizaje
  es un agujero real, no un detalle. Vale más el fix de 2 líneas + test que un número obtenido por la puerta de atrás.
- **Parar y loguear antes del re-run**: el fix del gap + su test se hacen mejor en contexto limpio, y el re-run
  del arco (~10min) se aprovecha para validar el fix Y sacar el `aiops_feedback_verdict_total{cured}` de una.

## Siguiente
1. **Fix del gap R2-human** (2 líneas + test): en `main.py:1234` pasar `doc_id=incident_doc_id_del_approve`
   y `remediation=remediation_for_feedback` a `_schedule_rollback_evaluation`, espejo del auto (main.py:956).
   OJO: el approve necesita reconstruir/recuperar el `doc_id` del incidente original — mirar si `incident`
   (la escalación) lo lleva o hay que derivarlo con `make_incident_doc_id` como en el pipeline. Test de paridad:
   approve → healthy → `aiops_feedback_verdict_total{cured}` sube y el doc se marca `cured`.
2. **Re-run corto del arco `cured`** con el fix → confirmar `aiops_feedback_verdict_total{outcome="cured"} 1.0`
   + doc en ChromaDB con `outcome=cured`. Con `cured`+`rolled_back` → **R4** (gráfica para el deck).
3. **Screenshot MM del mensaje de éxito `cured`** (Gate 8 — el positivo que faltaba).
4. **Cleanup**: `chaos-oom-target` quedó CURADO a 512Mi (Running estable) — al re-arrancar, borrar deployment +
   limpiar `rollback:*`/`escalation:*`/cooldown en Redis.
5. **Docs** (vía `/promote` futuro): C-02 ahora tiene causa raíz concreta (main.py:1234 sin doc_id); anotar el
   gotcha "narración del LLM ≠ acción" y el flakiness del port-forward+websocket de MM.
6. **Pendiente arrastrado**: `k8s/deployment-agent.yaml` tag `da7aafb→8a40fdc`; commit de Jay (promote + fix
   manifiesto + screenshot); F-11/F-17; matriz E1–E6 (`docs/14`); confirmar `aiops_remediation_rollback_total{healthy}=1`
   (Jay invocó /log antes de correr ese último grep).
