---
date: 2026-07-14
session: S8·a — forense incidente nocturno + estabilización pre-video
promoted: false
---

# 2026-07-14 · Incidente nocturno, forense del auto-bucle y estabilización pre-demo

## Objetivo

Día de grabación del video-demo (chapter mañana 15-jul). Plan en 5 mini-sesiones: (1) forense del incidente nocturno + lista de fixes escrita, (2) estabilizar el cluster vía manifiesto (apply-only), (3) actualizar y acortar el deck v3, (4) fixes de código SIN deploy, (5) pre-vuelo y grabación.

## Contexto de entrada

Anoche TargetDown [CRITICAL] flapping sobre el propio pod del agente (4 sufijos de pod distintos entre 23:36 y 05:29, mismo RS `75975b75b7`). A las 00:03 el agente se autodiagnosticó ("high CPU and memory") y escaló a humano proponiendo 4Gi/5Gi para sí mismo, `grounded=1.0`. Esta mañana: pod con RESTARTS=1, `lastState` 137/`Error` a los ~77s de arrancar, logs `--previous` congelados en "Waiting for application startup" (telemetría chroma), eventos "failed liveness probe". Drift detectado: manifiesto 384Mi vs deployment vivo 2Gi.

## Hecho

- Review crítica del diagnóstico inicial (sesión anterior de hoy): confirmó liveness-kill en cold start, tumbó "preemption de spot" (el agente está anclado a nodos guaranteed no-spot) y reencuadró el drift ("un leak cambia el uso, nunca el spec → hay un actor").
- Forense completo con RS history + managedFields + Cloud Logging (el TSDB de Prometheus se perdió — ver Encontrado #4).
- `docs/11` actualizado: nueva sección con **F-19..F-27** (dos findings nuevos de primera: F-26 gate de reason ausente, F-27 Prometheus volátil; F-23 revisado a no-aplica).
- **MS-2 (manifiesto)**: `k8s/deployment-agent.yaml` editado — startupProbe `/healthz` (period 10, failureThreshold 30 = 300s, timeout 3) + limit 2Gi reconciliado (F-19+F-22). Validado con dry-run client+server. Imagen intacta (`2ac3c5d`). Requests sin tocar (F-23 no aplica). Apply/verify = Jay.
- **MS-3 (deck v3)**: fusionadas Escenario+Propuesta (4 defs, ~40s), cadena completa de tiempos recalculada (cierre 11:40, total ~12:10 con el vídeo incluido; antes ~12:55), notes de Retrieval/Memoria aligeradas, presupuesto de portada y RUNBOOK actualizados a ~12 min. QA: la 23 reescrita (doble botón YA desplegado y validado S7, con el bug del underscore como propina) + 2 nuevas del incidente (self-target con la historia real 384Mi→10Gi; «fundada 100% ≠ causa verificada»). 25 QA. Regenerado y verificado (18 core + 4 respaldo, 0 menciones al presupuesto viejo). Decisión: NO se añadió la captura del doble botón a las slides — riesgo de layout la víspera; la captura queda como material de respaldo si preguntan.

## Encontrado (por orden de gravedad)

### 1. El agente llevaba una semana auto-remediándose a sí mismo (F-21 evidenciado)

Historial de ReplicaSets del deployment `agent` (cada cambio de spec = RS nuevo):

| Fecha (Z) | rev | mem limit |
|---|---|---|
| 07-04 15:01 | 58 | 384Mi (manifiesto) |
| 07-04 15:08 | 59 | 512Mi |
| 07-06 08:36 | 61 | 512Mi |
| 07-06 09:31 | 62 | 1Gi |
| 07-06 11:16 | 67* | 2Gi (*RS reutilizado en un revert posterior) |
| 07-07 06:00 | 64 | 4Gi |
| 07-07 12:50 | 65 | 5Gi |
| 07-07 21:03 | 66 | **10Gi** |
| (revert) | 67 | 2Gi |

`managedFields`: el dueño de `limits.memory` es **`kubectl-set`** — el comando del propio motor. Smoking gun en Cloud Logging (07-07 06:00:22-23Z, sin ningún humano a las 6 AM):

```
Diagnosis generated: confidence=0.85 risk=high duration=160995ms
seal_proposed_action: engine-authored new_value (LLM value unusable) {current_value: 2Gi, model_new_value: 1Gi, new_value: 4Gi}
ground_confidence: grounded from cluster snapshot {grounded_confidence: 1, last_state_reason: "Error", restart_count: 3}
Remediation command succeeded {command: kubectl set resources deployment agent -n arturo-llm-test --containers=agent --limits=memory=4Gi}
Remediation decision {action: auto_remediate, confidence: 1, risk: high}
```

Léelo despacio porque tiene tres capas: (a) la alerta era `KubePodCrashLoopBackOff` del **propio agente**, causada por el bucle de infra (churn→cold start→liveness kill), no por memoria; (b) `last_state_reason="Error"` estaba EN el snapshot y nadie lo miró — ni el gate del motor (F-26) ni el LLM (que además propuso **bajar** a 1Gi; el motor lo marcó inusable y dobló a 4Gi); (c) `grounded=1.0` — el grounding avala existencia del target, no la causa (F-25). El bucle completo: **bug de infra → CrashLoop → auto-misdiagnóstico como memoria → self-patch → rollout → más restarts → repeat**. Anoche NO se repitió únicamente porque TargetDown no es estructurado → escaló en vez de auto.

### 2. Mecanismo del churn nocturno: upgrade GKE + preemption por prioridad (no spot, no evictions)

- Los 2 nodos guaranteed fueron **reemplazados hoy** (creados 12:37Z y 13:48Z, v1.36.0-gke.4447000); anoche la ventana de mantenimiento hizo lo propio → 4 recreaciones del pod.
- Eventos en vivo esta mañana: 3 preemptions del agente en ~7 min — `Preempted by pod <uid>` = **kube-dns, priority 2000000000** (system-cluster-critical), con `FailedScheduling: Insufficient cpu`. Durante cada reemplazo de nodo todo se apiña en el superviviente (requests CPU al 78%+ en e2-standard-2) y los pods críticos expulsan al agente (priority 0).
- Cloud Logging nocturno: 1 evento `Preempted` (05:56Z), **0 Evicted, 0 OOMKilling** → OOM descartado con la evidencia disponible; F-23 (subir requests) revisado a no-aplica: contra preemption por prioridad no defiende y agrava el Insufficient cpu.
- **Los nodos nuevos ya no tienen el taint `guaranteed`** (la label sí — el nodeSelector sigue funcionando) → pods de otros tenants pueden aterrizar ahí. Nota operativa, no accionable hoy (taint de nodo = operación sobre infra compartida).

### 3. La muerte de las 08:47: cold start colgado, no OOM (F-19 + F-20)

Confirmado contra el código: uvicorn no abre :8000 hasta terminar el lifespan; el lifespan es fail-open **solo si las llamadas fallan** — `chromadb.HttpClient` (rag.py:63) y `redis.ping()` (main.py:577) no tienen timeout, y con el servicio destino sin endpoints la conexión puede colgar (blackhole) en vez de fallar → el `except` nunca salta → 65s de silencio → liveness (ventana ~55s) mata. Ollama exonerado (timeout=10s explícito). Con deps calientes el startup tarda 3s (visto en logs del 07-07). Fix hoy: startupProbe (F-19, apply-only). Fix código: acotar startup (F-20).

### 4. El TSDB de Prometheus se evaporó (F-27)

Prometheus es stateless; se reprogramó con el churn (~14:00Z) y **toda la historia murió con el pod**: el forense nocturno hubo que hacerlo por Cloud Logging, y la serie R5 del Gate 8 (`aiops_incident_resolution_seconds{OOMKilled}=92.47s` del 07-13) **ya no existe**. La nota del roadmap "la serie está en el TSDB, no se pierde con reinicios del agente" era cierta pero incompleta: no sobrevive a reinicios de *Prometheus*. Gate 8 pasa a capturarse EN VIVO durante el arco del video (la Escena 6 regenera la serie). Post-chapter: PVC para Prometheus.

## Decisiones (con porqué)

1. **Hoy solo manifiesto al cluster** (startupProbe + reconciliar 2Gi); los fixes de código (F-20/F-21, y F-26 si entra) se escriben y testean hoy pero NO se despliegan — la imagen `2ac3c5d` está validada E2E y el día antes del chapter no se estrena imagen. (Decisión de Jay en la planificación.)
2. **F-23 descartado tras el forense**: la hipótesis eviction-por-memoria era falsa (eran preemptions por prioridad). Lección de método: el fix condicional se condicionó a evidencia y la evidencia dijo no.
3. **2Gi como valor de reconciliación** aunque sea un artefacto del auto-bucle: headroom inofensivo para hoy; right-sizing con datos frescos post-chapter.
4. **F-26 elevado a Alta**: es el gate que habría roto el bucle en la raíz (C-01 ya mira `lastState.reason` en el rollback; el motor no lo mira en la entrada). Candidato a entrar en MS-4 junto a F-21 (misma zona de código).
5. **Incidente al deck solo como QA** (decisión de Jay): munición honesta sin alargar. El relato es doble: anoche el sistema hizo lo correcto (escaló), y hace una semana hizo lo incorrecto (auto-bucle) — y ambas cosas están medidas y con fix escrito.
6. **Gate 8 en vivo**: la captura de Grafana R5 se hace durante el arco de la grabación, no desde histórico (que ya no existe).

## Siguiente

- **PRÓXIMA SESIÓN (/start): capturar el video-demo. Objetivo del arco: veredicto `cured` en pantalla.** Regla de oro aprendida en S7: con el manifiesto chaos actual (stress 100M), el botón del motor (×2=64Mi) acaba en `rolled_back`; **el que cura es `approve_model` (512Mi)** → en la Escena 4 del guion se aprueba EL DEL MODELO. Pre-vuelo completo en `demo/guion_video.md` + gotchas S7 (Redis limpio, ollama warm por port-forward, teardown como último gate). Gate 8 en vivo durante este mismo arco (captura Grafana de `aiops_incident_resolution_seconds` — el TSDB ya no tiene la serie del 07-13).
- **Jay, antes de grabar**: `kubectl apply -f k8s/deployment-agent.yaml` + rollout status + `/readyz` (comandos en el chat de la sesión); verificar estabilidad ≥30 min; NO lanzar el smoke de alerta (siembra cooldown+índice en Redis).
- **Pendiente de Jay**: `python3 -m pytest agent/tests/ -q` para re-confirmar el número de `STATS['tests']` (el deck dice 696) — si difiere, tocar `build_demo_v3.py:43` y regenerar.
- MS-4 (no arrancada): código F-20 + F-21 (+F-26, recomendado incluirlo — mismo área) con tests, SIN deploy hasta post-chapter.
- MS-5: pre-vuelo `guion_video.md` + grabación + **Gate 8 EN VIVO** (la serie R5 se regenera con el arco del video; capturar Grafana en el momento — el TSDB ya no tiene la del 07-13).
- S8·b (mañana pre-chapter): `/ensayo` hostil con munición nueva (incidente nocturno, dos QA nuevas); checklist offline; vault end-session (tabla Vault Impact arriba).

## Vault Impact

| Tipo | Nota destino | Contenido |
|---|---|---|
| Lesson | 03_Knowledge/AI_ML/ | Self-targeting agents: un agente de remediación sin regla de auto-exclusión convierte un bug de infra en un bucle de auto-medicación (evidencia real: 384Mi→10Gi en 3 días). "El médico no se opera a sí mismo" |
| Lesson | 03_Knowledge/Programming/ | Fail-open sin timeouts es fail-open de mentira: la rama except solo protege si la llamada FALLA; una conexión colgada (blackhole) la esquiva. Acotar SIEMPRE el startup |
| Lesson | 05_Reflections/ | La observabilidad también necesita durabilidad: un TSDB stateless convierte "la métrica está guardada" en "la métrica está guardada hasta el próximo churn". Capturar evidencia EN el momento (Gate 8 en vivo) |
| Pattern | 04_Systems/Patterns_I_Keep_Using.md | Forense sin métricas: RS history (spec changes con timestamp) + managedFields (actor) + Cloud Logging (qué hizo el proceso) reconstruyen un incidente aunque el TSDB haya muerto |
| Project | 01_Projects/ (nodo AIOps) | F-19..F-27; incidente nocturno como munición QA del chapter |
