---
fecha: 2026-07-04
slug: cluster-validation-v2-f03
promoted: true
---

## Objetivo
Sesión de dos frentes en paralelo: (1) Claude cierra **F-03** (ChromaDB no bloqueante,
único Alta de puro código pendiente); (2) Jay ejecuta el sprint cluster S1→S3 (pytest +
commit + push + secrets + build + deploy) y **valida por primera vez en cluster todo el
delta v2** (salto `da7aafb → 8a40fdc`: Eje A grounding + Eje B + hardening + F4 R1/R2/R2·3/R3
+ F-01/02/04/05 + F-03). Nunca se había corrido la v2 en cluster.

## Hecho
- **F-03 (código)**: envueltas TODAS las llamadas síncronas de ChromaDB (HTTP bloqueante)
  en `asyncio.to_thread` para no congelar el event loop:
  - `rag.py`: `retrieve_context` (bloque chroma completo → inner `_blocking` offloadeado,
    conserva two-stage R1 y filtro R2·3), `ingest_runbook` + `ingest_incident` (`_upsert`
    en thread). `import asyncio` añadido.
  - `main.py`: `ensure_collections` en lifespan → `to_thread`; call site de
    `_query_recent_incidents` (slash `/aiops`) → `await asyncio.to_thread(...)`.
  - `_reupsert_incident_outcome` (R2) ya pasa por `ingest_incident` → cubierto.
  - +2 tests `TestChromaOffloading` (spy sobre `rag.asyncio.to_thread`, verifica offload
    + resultado intacto). Barrido confirmó cero llamadas chroma síncronas sueltas en async.
- **Jay (cluster)**: `pytest` → **620 verde** (reconcilia el recuento: los docs decían ~613;
  **el número real es 620**) → commit único (F-03 + F-01/02/04/05 + guard secrets + CI) +
  push (estrena CI GitHub Actions) → recuperación de secrets OK (webhook URL + token reales)
  → Cloud Build **SUCCESS `aiops-agent:8a40fdc`** → `set image` + rollout OK.
- **Screenshot Gate 8 guardado**: `demo/mattermost_escalation_grounded_confidence.png`
  (mensaje MM con "Confidence: 100% (grounded del cluster; el modelo dijo 95%)" + comando
  determinista `--limits=memory=512Mi`). Convive con los `grafana_*.png` del deck.
- **Chaos S3 ejecutado** (3 corridas): 1ª con `scripts/chaos.sh` (destapó problemas del
  harness), reset de cola + restart agente, 2ª+3ª manual → validación del arco v2.

## Validado EN CLUSTER (1ª vez — el payoff de la sesión)
- 🎯 **Grounding Eje A**: `enrichment: snapshot gathered … last_state_reason=OOMKilled,
  workload_kind=Deployment, workload_name=chaos-oom-target` (identidad por ownerReferences,
  pod vivo). `seal_proposed_action: sealed from cluster snapshot … current_value=32Mi`
  (el valor sale del CLUSTER, no del LLM). `ground_confidence: grounded=1.0, model=0.95`.
  **La clase de fallo `NotFound`/alucinación del slice 6 está MUERTA en el camino grounded.**
- ✅ **Safety cap (regla 4.6)**: el LLM propuso `512Mi` (16× el límite de 32Mi) →
  `Remediation blocked: proposed limit exceeds 2x current` → **escalate** (no auto-patch).
  El cap NO clampa en silencio (acuerdo tutor "overshoot >2× escala").
- ✅ **Human-in-the-loop E2E**: escala → **approve** (HMAC OK ⇒ secrets recuperados correctos)
  → `[OK] kubectl set resources … 512Mi` → `deployment … resource requirements updated`.
  512Mi persistió. Botón ✅ validado.
- ✅ **Rollback durable (P0·3)** + **R2**: el approve programó rollback (paridad humano/auto);
  +300s → eval → revert → MM → veredicto re-upsert. Toda la maquinaria disparó.
- ✅ **R1** (1 runbook + 2 incidents, filtro error_class), **R2** (verdict `cured` re-upsert
  de un TargetDown previo), **Gate E4** (`feedback_total{skipped}` en diagnósticos sin
  confidence), **F-03** (chroma offload transparente, cero errores), cola/dedup/reclaim,
  MM, confidence grounded visible.

## Encontrado / gotchas (todos con causa raíz, ninguno bloqueante de diseño)
- **LLM = cuello de botella, techo de HARDWARE**: qwen2.5:1.5b en CPU tarda **147-213s warm**
  por diagnóstico; **timeoutea a 360s en cold** (1ª llamada tras deploy). Ollama ya tiene
  `cpu: 2` = **todo el nodo e2-standard-2** (2 vCPU, sin margen), modelo warm (keep-alive 24h),
  100% CPU sin GPU. NO bajable con más CPU, NO es cold-start puro. Es infra sandbox, no diseño.
  MTTD=5s (detección instantánea), MTTR dominado por el LLM.
- **`scripts/chaos.sh` NO sirve para validar el arco**: su ciclo de auto-cleanup (~300s) es
  **más corto que el arco completo** (patch → 300s ventana rollback → veredicto ≈ 10 min) →
  borra el deployment a mitad → `NotFound` en `capture_pre_patch`/remediación/rollback.
  Además, sin pod vivo la enrichment del CrashLoop cayó a `skipped` → seal sin snapshot →
  valor sin grounding (512Mi del LLM). **Para validar el arco: aplicar el manifiesto a mano.**
- **FALSO ROLLBACK (el hallazgo clave)**: tras aprobar 512Mi, el rollback eval dio
  `healthy=false, reason="pods_restarting: [3]", phases=["Running"]` → revirtió a 32Mi
  (`outcome=reverted`). El pod **NO OOMeaba** a 512Mi — los 3 restarts eran del ciclo
  `stress --timeout 60` (stress sale limpio cada 60s → contenedor reinicia por exit limpio).
  El health-check **cuenta restarts sin mirar el motivo** → malinterpreta exit-limpio como
  crash. **Artefacto del MANIFIESTO, no del sistema**: en un OOM real, fix que cura = 0
  restarts = healthy; fix que falla = sigue OOMeando = revert. El heurístico es correcto
  para OOM real.
- **Doble escalación misma causa raíz**: `KubePodOOMKilled` + `KubePodCrashLoopBackOff` sobre
  el mismo pod → dos escalaciones para un problema. El cooldown F-01 lo habría deduplicado
  **si el primero se hubiera auto-remediado** — pero fue approve humano, y hoy el approve
  **no siembra cooldown**. Evidencia directa para cerrar la decisión pendiente.
- **Escalación TTL = 60min** (`ESCALATION_TTL_MINUTES`): aprobar un mensaje de hace >60min →
  `get_escalation` None → "Escalación no encontrada o expirada". No es bug (nos pasó por
  aprobar 2h tarde). Posible subida a 2-4h para demos/ausencias.
- Menores: telemetría `chromadb-client` "Failed to send telemetry event" (benigno, PostHog
  sin internet); el diagnóstico del 1.5b repite el nombre del pod dos veces (artefacto,
  no bloqueante); `chromadb-client` (no `chromadb` full) es la dependencia correcta/intencional.

## Decisiones + por qué
- **F-03 en el commit único, no aparte**: mecánico y 100% mockeado → viaja con el resto y se
  valida gratis en el mismo chaos. Confirmado transparente (0 regresión, 620 verde).
- **NO meter F-11/F-17 en la build de validación**: tocan la fontanería (`doc_id`/`incident_id`)
  y el prompt del LLM — justo lo que el chaos valida (R2 por doc_id, grounding). Meterlos
  contaminaría el keystone y dificultaría bisectar un fallo. Deferidos a post-chaos.
- **Aprobar 512Mi aunque supere 2×**: es el valor que CURA (512 > 100M que pide el stress);
  el auto capado a 64Mi ni siquiera curaría → rollback. El escalate + approve humano es
  precisamente la razón de ser del human-in-the-loop para saltos grandes. Buena historia deck.
- **Decisión pendiente cerrada de facto — el approve humano SÍ debe sembrar cooldown**: la
  doble escalación OOM+CrashLoop lo demostró en vivo.
- **Parar aquí y hacer el `cured` en sesión limpia**: la validación ya es abrumadora; el
  único hueco (`cured` positivo) requiere arreglar el manifiesto y un run controlado, mejor
  con contexto fresco "de una".

## Siguiente (próxima sesión, contexto limpio — hacerlo "de una")
1. **Editar `k8s/chaos/chaos-oom.yaml`**: quitar `--timeout` del stress (→ stress infinito) →
   OOM real (crash-loop de verdad) SIN restarts benignos. Es el fix del falso-rollback.
2. **Run limpio del arco `cured`** (a mano, NO `scripts/chaos.sh`): `kubectl apply` →
   esperar OOM → escalar → **aprobar 512Mi RÁPIDO** (<60min TTL) → esta vez pod estable →
   health-check `healthy` → **veredicto `cured`** → `feedback_verdict{cured}` +1.
   Screenshot MM "APROBADA/cured". Con `cured` + `rolled_back` juntos → **R4** (gráfica
   `aiops_feedback_verdict_total` para el deck).
3. **Cleanup**: `kubectl delete -f k8s/chaos/chaos-oom.yaml` + limpiar cooldown/escalaciones
   residuales en Redis. (OJO: ahora mismo `chaos-oom-target` quedó a 32Mi y sigue OOMeando —
   limpiar al empezar.)
4. **Anotar hallazgos en docs canónicos** (vía `/promote`):
   - Health-check del rollback: mirar `lastState.reason==OOMKilled`, no solo contar restarts
     (v2.x — evita falsos rollbacks en workloads que reinician por motivos benignos).
   - Approve humano siembra cooldown (extensión F-01 — cierra la doble escalación).
   - `ESCALATION_TTL_MINUTES` 60→¿120-240? para demos.
   - Reconciliar recuento de tests a **620** en `docs/07` + CLAUDE.md.
   - `scripts/chaos.sh` no observa el arco (ciclo cleanup < 10min): documentar "run manual
     para validar arco".
5. **Pendiente arrastrado**: Gate 8 resto de screenshots (Grafana), matriz E1–E6 (`docs/14`),
   `/promote` de las bitácoras `promoted:false`, F-11/F-17, deck (3 slides review).
6. **Jay housekeeping**: `k8s/deployment-agent.yaml` sigue apuntando a `da7aafb` (desplegado
   `8a40fdc`) — actualizar en el siguiente commit; commitear el screenshot + el cambio de
   manifiesto del chaos.
