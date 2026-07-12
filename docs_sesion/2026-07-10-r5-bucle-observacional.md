---
fecha: 2026-07-10
slug: r5-bucle-observacional
promoted: false
---

## Objetivo
**Bloque 2** del plan pre-chapter (4 bloques): **R5 — las alertas `resolved` de
Alertmanager cierran el bucle observacional**. Correlar por fingerprint la alerta que
se resuelve con el incidente que la disparó → outcome `resolved_observed` en ChromaDB +
métrica de tiempo de resolución. Es la pata que da un outcome **real y barato** para
*todas* las clases de alerta (no solo memoria) — tapa justo el borde honesto de
"safety ≠ correctness" para no-memoria que quedó abierto en la sesión demo-polish.

## Hecho
- **`agent/incident_index.py` (nuevo)** — índice de correlación fail-open en Redis
  (espejo de `escalation_store.py`): `record_active_incident` / `pop_active_incident`,
  clave `incident:active:{fingerprint}` con TTL nativo. `pop` hace GET+DEL (una
  resolución se consume una sola vez). Redis None / caído → default seguro, nunca
  raisea.
- **R5 en `main.py`**:
  - Helper `_alert_fingerprint(alert)` = `{alertname}:{namespace}:{pod}` — **fuente
    única** compartida por el enqueue (dedup) y las dos patas de R5. Refactoricé el
    enqueue para usarlo (antes replicaba la fórmula inline → riesgo de divergencia
    silenciosa que rompería la correlación).
  - **Ingest**: tras persistir el incidente en ChromaDB, se indexa por fingerprint con
    `{doc_id, error_class, started_at, awaits_verdict, text, metadata}`.
    `awaits_verdict = rollback_scheduled`.
  - **Resolved**: la rama `resolved` del webhook lanza `_correlate_resolution(fp)` como
    background task (junto al notify a Mattermost). Hace pop del índice → si hit: emite
    la métrica de resolución; si además `awaits_verdict=False`, re-upserta el doc a
    `resolved_observed` (reusa el `text`+`metadata` guardados; añade línea "Resolution:
    alert cleared after Ns").
  - 2 métricas nuevas: `aiops_incident_resolution_total{correlated=hit|miss}` +
    `aiops_incident_resolution_seconds{error_class}` (histograma, buckets 60..3600).
- **`agent/rag.py`**: constante `INCIDENT_OUTCOME_RESOLVED_OBSERVED = "resolved_observed"`.
  Es retrievable — `INCIDENTS_RETRIEVAL_FILTER` solo excluye `auto_pending`.
- **`agent/config.py`**: `incident_correlation_ttl_seconds = 3600` (env-overridable).
- **Tests (`agent/tests/test_incident_index.py`, nuevo, ~18)**: módulo (roundtrip,
  pop consume, fail-open ×3), `_correlate_resolution` (hit no-auto re-upserta / hit
  awaits solo métrica / miss / campos incompletos / fail-open ante ingest caído /
  started_at no numérico), `_alert_fingerprint`, y wire en
  `_process_alert_with_diagnosis` (un escalate queda indexado con awaits_verdict=False).
  **175 passed** (pytest global de Jay).

## Decisiones + por qué
- **`awaits_verdict` — el veredicto del rollback es dueño del outcome**. `resolved_observed`
  es una señal **débil** (la alerta dejó de dispararse, pero la causa no está verificada:
  pudo curarla un humano, pudo auto-sanarse). `cured`/`rolled_back` son **fuertes**
  (verificamos salud del pod + fue nuestro fix). Por eso R5 solo **escribe outcome** para
  incidentes que NO entraron al bucle de rollback (escalados / suggest-only) — que son
  justo los que hoy se quedaban sin outcome real. Para los auto, `resolved` solo emite la
  métrica y **nunca pisa** el veredicto. Refuerza la tesis "el motor dispone": la señal
  fuerte la impone el motor, la observacional la complementa sin degradarla.
- **Cierre de la race del approve humano**: el approve indexa (en diagnóstico, como
  escalate) con `awaits_verdict=False`, pero **luego** programa un rollback → si el
  `resolved` llegaba tras el veredicto `cured`, lo degradaba a `resolved_observed`. Fix:
  cuando el approve programa el rollback, **suelto la entrada del índice**
  (`pop_active_incident`, fail-open) → el veredicto pasa a ser dueño único. Pierdo la
  métrica de resolución de ese incidente (aceptable: ya tiene la señal fuerte). La rama
  auto de diagnóstico ya era correcta (indexa DESPUÉS de saber `rollback_scheduled`).
- **Guardar `text`+`metadata` en el índice** en vez de rehidratar diagnosis/remediation:
  el doc ya está construido en el ingest; re-upsertar = flip de `outcome` + append de una
  línea. Sin rebuild, sin arrastrar objetos no serializables. Unos cientos de bytes por
  incidente activo, con TTL.

## Encontrado / gotchas
- El nombre "R5" colisiona con un **finding viejo ya DONE** en `docs/11:31` (renombrado
  `executed→execution_attempted`). Este "R5" es el **Bloque 2** del plan pre-chapter, no
  un finding del backlog — no hay fila que cerrar en docs/11.
- La correlación depende de que el fingerprint del `firing` y del `resolved` **coincidan
  exactamente** → de ahí `_alert_fingerprint` como fuente única (un cambio en una pata
  sin la otra rompería la correlación en silencio, sin error).

## Hecho — F-17 (Bloque 3, primera mitad)
- **Logs + events del pod al snapshot/prompt** — el 1er uso genuinamente **nuevo** del LLM
  (razona sobre lenguaje libre, cero ejecución). `enrichment.py`:
  - `IncidentSnapshot` gana `logs_tail: str | None` + `recent_events: list[str]`.
  - `_kubectl_text` (hermano de `_kubectl_json` para salida no-JSON: logs) — misma
    invocación argv segura, timeout corto, reaping del hijo, fail-soft.
  - `_gather_logs`: `kubectl logs --tail=N`, usa **`--previous`** si el contenedor terminó
    (`last_state_reason`) o reinició (`restart_count>0`) — ahí está la traza del OOM/crash,
    el contenedor fresco no logea nada; **fallback** a current si no hay previous. Cap por
    líneas (`--tail`) y por chars (cap duro del blob).
  - `_gather_events`: `kubectl get events --field-selector involvedObject.name=<pod>
    --sort-by=.lastTimestamp -o json` → "Type/Reason: message", newest-last, límite N.
  - Ambos cableados al final de `gather_incident_context`, cada uno fail-soft por separado.
- `diagnosis.py`: `format_cluster_facts` gana bloques delimitados **RECENT EVENTS** /
  **RECENT LOGS** (separados de CLUSTER FACTS para que los hechos estructurados no se
  mezclen con texto libre). Si solo hay logs/events (sin hechos estructurados) igual
  renderiza.
- `config.py`: `enrichment_log_tail_lines=20`, `enrichment_log_max_chars=2000`,
  `enrichment_events_limit=5`.
- Tests: `test_enrichment.py` (+`TestGatherLogs`/`TestGatherEvents`/`TestGatherWires…`,
  ~14) + `test_diagnosis.py` (RECENT LOGS/EVENTS en `format_cluster_facts`). Los tests
  existentes de gather siguen verdes: las 2 llamadas nuevas (logs+events) sobre un
  `_seq` agotado dan None/[] fail-soft, y no hay asserts de call_count.

## Decisiones + por qué (F-17)
- **`--previous` cuando el contenedor murió**: en un OOM/CrashLoop el contenedor vivo es
  un reemplazo fresco sin logs útiles; la traza está en la instancia anterior. Heurística
  determinista (reason o restart>0), con fallback a current si no hay previous (primer
  crash aún sin reiniciar). Sin esto, F-17 traería logs vacíos justo en los casos que
  importan.
- **Bloques separados en el prompt**: CLUSTER FACTS (autoritativo, structured) vs RECENT
  LOGS/EVENTS (observado, texto libre) — no mezclar para no diluir la autoridad de los
  hechos sellados que el motor usa.
- **Caps duros** (líneas + chars + nº eventos): el prompt del 1.5b tiene presupuesto de
  tokens estrecho; logs sin acotar lo revientan. Fail-soft en cada gather por separado.

## Hecho — C-07 (Bloque 3, segunda mitad)
- **Pre-flight `kubectl auth can-i` para comandos free-text** (factibilidad ≠ seguridad).
  El problema: la validation layer clasifica `kubectl top pod` como SAFE (read-only), pero
  bajo least-privilege la SA no tiene `metrics.k8s.io` → Forbidden al ejecutar. El fix mueve
  el fallo de "ejecutar-y-fallar" a "sugerir sin permiso". `remediation.py`:
  - `auth_can_i_args(cmd)` (puro, testeable): mapea la forma SAFE a `(verb, resource,
    cluster_scoped)`. Clave: `top pod`→`get pods.metrics.k8s.io`, `top node`→
    `get nodes.metrics.k8s.io` (cluster-scoped), `describe/get X`→`get X`, `logs`→`get pods`,
    `version`→None (sin RBAC). Lo no parseable → None (el llamante asume ejecutable).
  - `check_command_executable(cmd, ns)`: corre `kubectl auth can-i <verb> <resource> [-n ns]`;
    **False solo ante un "no" explícito**; True en cualquier otro caso (no parseable, sin
    RBAC, o error/timeout → fail-open, nunca bloquea por un fallo del propio check).
  - `partition_by_permission(cmds, ns)` → `(executable, denied)`, order-preserving.
- **`main.py`** (rama de escalación free-text): pre-flighta los `safe_commands`; los
  **executable** son los aprobables (botones), los **denied** se muestran como "comandos
  sugeridos (el agente no tiene permisos para ejecutarlos)". Si TODOS son denied → no hay
  escalación con botones, cae a notificación con los sugeridos. El comando **estructurado**
  del motor (memoria) es determinista/conocido-ejecutable → **no pasa por el check** (cero
  riesgo para el camino validado en cluster). Gate solo en acción ESCALATE (no añade
  llamadas kubectl al camino SUGGEST/AUTO).
- `_format_escalation_body(diagnosis, remediation, denied_commands=None)`: separa
  aprobables (requieren aprobación) de sugeridos (sin permisos).
- Tests: `test_remediation.py` (+`TestAuthCanIArgs`/`TestCheckCommandExecutable`/
  `TestPartitionByPermission`, ~15) + `test_endpoints.py` (2 wiring: all-denied→sugerencia
  sin botones; parcial→filtra el aprobable y la escalación persiste solo el ejecutable).

## Decisiones + por qué (C-07)
- **Fail-open salvo "no" explícito**: si el propio `auth can-i` falla (timeout, sin cluster,
  parseo raro), el comando sigue ejecutable. Bloquear por un fallo del check reintroduciría
  el problema que arreglamos (ocultar acciones válidas). Solo un "no" de RBAC degrada.
- **Solo camino free-text, solo ESCALATE**: el comando de memoria es sellado por el motor y
  conocido-ejecutable → saltárselo evita latencia y riesgo en el camino validado. Gatearlo a
  ESCALATE evita round-trips kubectl en SUGGEST/AUTO.
- **NO ampliar RBAC** (convención: sin ClusterRoles de escritura, least-privilege): el fix es
  *honestidad* (mostrar lo que no se puede ejecutar), no permisos nuevos.
- **Descartado**: gatear en `execute_commands` (execution-time). El intent de C-07 es
  pre-flight en el punto de OFRECER (que el humano no apruebe un botón que fallará), no
  fallar al ejecutar. El pre-flight en la construcción de la escalación cumple eso.

## Siguiente
1. **Bloque 4**: C-08 (doble botón model/×2) + F-06 (docs durabilidad Redis) + `/ensayo`.
2. **Deck**: nuevas QA/slides — R5 como el outcome real para no-memoria (MTTR observado);
   F-17 (logs+events = 1er uso nuevo del LLM); C-07 (factibilidad ≠ seguridad, el agente es
   honesto sobre lo que no puede ejecutar). Candidatas a slide "cómo cerramos el bucle".
3. **Arrastrados**: matriz E1-E6 (docs/14); Gate 8 screenshots; `/promote` masivo
   (07-07, 07-08, 07-09 ×2, este 07-10). Commits: R5 ya en `1dbfdf3`; falta commitear F-17
   + C-07 + docs de sesión.

## Vault Impact
| Archivo | Cambio |
|---|---|
| 03_Knowledge/AI_ML/ (patrón) | Jerarquía de señales de outcome en un agente que actúa sobre infra: verificada-por-el-motor (fuerte) > observada (débil); la débil complementa pero nunca degrada la fuerte |
| 03_Knowledge/AI_ML/ (patrón) | Factibilidad ≠ seguridad: un clasificador de "seguro" (read-only) no garantiza "ejecutable" (RBAC); un agente honesto pre-vuela permisos y sugiere lo que no puede correr en vez de fallar en ejecución |
| 01_Projects/AIOps node | Bloques 2 (R5) + 3 (F-17 logs+events, C-07 auth can-i) cerrados en código + tests. Queda Bloque 4 (C-08 + F-06 + ensayo) |
