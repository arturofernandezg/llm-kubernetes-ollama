---
fecha: 2026-07-02
slug: review-senior-fixes-altas
promoted: true
---

## Objetivo
Sesión autónoma ("ve decidiendo tú, sin microtasks"): leer la review senior modo-libro
(`docs_sesion/estado_de_proyecto.html (antes 2026-07-02-paper-review-senior.html)`, 7.1/10, 18 hallazgos F-01..F-18) y
atacar lo que ella misma marca como pequeño + alto + testeable con mocks, mientras el
despliegue de `cb2d1db` sigue bloqueado en la recuperación de secrets (manual, Jay).

## Hecho
- **Guard anti-placeholder en `k8s/secrets-setup.sh`** (pendiente de la sesión anterior,
  refuerza F-16): función `abort_if_placeholder` + 2 llamadas (webhook URL, mm-command-token).
  Cualquier `<...>` sin sustituir aborta ANTES de tocar kubectl. Verificado en aislado:
  placeholder → exit 1; valor real → pasa. `bash -n` OK.
- **F-05 (Alta) — dedup-key marcada antes del XADD** (`agent/streams.py::enqueue_alert`):
  si el XADD falla tras el SETNX, ahora se borra la dedup-key (compensación) antes de
  propagar. Sin esto, el retry de Alertmanager chocaba con la clave huérfana → 200
  "duplicado benigno" sin encolar → alerta perdida de verdad hasta el `repeat_interval`.
  El delete de compensación va en try/except con warning (fail-soft del cleanup).
- **F-02 (Alta) — kind resuelto pero no aplicado** (`agent/remediation.py::seal_proposed_action`):
  gate estricto `workload_kind == "Deployment"` — un STS/DS confirmado (o kind=None) anula
  la `proposed_action` + marker `target_unresolved` → regla 4.7 escala. Antes, un pod de
  StatefulSet disparaba el camino auto completo y moría en
  `kubectl set resources deployment <nombre-del-sts>` → NotFound (el MISMO síntoma que la
  v2 acaba de anunciar como resuelto, por otro camino).
- **Tests**: +1 `test_non_deployment_kind_drops_action` (parametriza STS/DS/None) en
  `TestSealProposedAction`; 3 fixtures existentes del seal actualizados para declarar
  `workload_kind="Deployment"` explícito (el contrato ahora lo exige). Falta el test de
  F-05 (compensación) — quedó a medias al cortar la sesión.
- **F-01 (Alta) — cooldown por workload**: EN CURSO, diseño cerrado (ver Decisiones),
  sin código aún. Config `remediation_cooldown_seconds: 600` decidido pero no aplicado.

## Encontrado / gotchas
- La review es post-hardening (`cb2d1db`): los 18 hallazgos NO incluyen los P0/P1 ya
  cerrados; los 6 Alta son los reales pendientes. H0 (antes del chapter) = secrets +
  deploy + chaos + Gate 8 + `/promote`; H1 = F-01..F-06 + F-11 + F-17.
- Los fixtures de `TestSealProposedAction` construían snapshots con `workload_name` pero
  sin `workload_kind` — en producción `_resolve_workload` los sella SIEMPRE juntos
  (enrichment.py:199-200), así que kind=None solo existe en snapshots artesanales de test.
  Se eligió gate estricto igualmente (None → escala): más defendible que el laxo.
- La métrica `aiops_enrichment_total` clasifica por `workload_name` (main.py:822) → un STS
  confirmado cuenta como `gathered` aunque el seal luego anule la PA. Es correcto (la
  métrica mide la etapa de gather, no la decisión); el drop se ve en el reason_code de la
  escalación. Decidido NO tocarla.
- El classifier de permisos denegó ejecutar `secrets-setup.sh` entero para probar el guard
  (tocaría el cluster si el guard fallara — razonable); se verificó la función en aislado.

## Decisiones + por qué
- **Orden de ataque**: guard → F-05 → F-02 → F-01 → F-04. Criterio de la propia review
  (H1 "pequeños y testeables con mocks") + lo del cluster está bloqueado en Jay. F-05
  primero por ser el agujero de 1 línea en el claim más fuerte del sistema ("no se pierde
  ninguna alerta"); F-02 porque reproduce el síntoma NotFound que la demo va a presumir
  de haber matado.
- **F-05, compensar (delete) en vez de invertir el orden (XADD→SETNX)**: invertir abre
  ventana de duplicados en ráfagas (N replicas / reenvíos concurrentes encolan antes del
  SETNX); la compensación mantiene la semántica actual y solo actúa en el caso raro de
  XADD fallido. Trade-off aceptado: si el delete de compensación TAMBIÉN falla, la alerta
  queda suprimida hasta expirar la ventana (warning explícito).
- **F-02, gate en el seal (no en `is_structured_remediation`)**: el seal ya es el dueño del
  contrato "target confirmado por el cluster" y su drop enruta por la 4.7 con razón visible;
  meterlo en `is_structured_remediation` habría dejado viva la PA sellada de un STS y el
  free-text del LLM como fallback. Estricto (kind≠Deployment INCLUYE None) porque producción
  nunca produce name-sin-kind; los tests que lo hacían se actualizaron para ser explícitos.
- **F-01 (diseño, pendiente de implementar)**: adquisición `SETNX aiops:cooldown:{ns}/{name}`
  con TTL `remediation_cooldown_seconds=600` (> ventana rollback 300s) DENTRO de
  `process_remediation`, solo en la rama auto estructurada (única que patchea; los safe
  commands son investigativos). `redis_client=None` → sin gate (tests/local); error de
  Redis → fail-closed a ESCALATE ("ante duda, escala" — y si Redis está caído la cola
  tampoco entrega, así que el caso real es un hiccup transitorio). El humano es la válvula
  de escape: la escalación lleva el comando determinista + botones. Pendiente decidir si
  el approve humano también siembra el cooldown (coherente, pero puede esperar).

## Siguiente
- **F-01**: implementar el diseño de arriba — config `remediation_cooldown_seconds=600`,
  helper de adquisición en `remediation.py` (o módulo pequeño), downgrade a ESCALATE con
  `reason_code=workload_cooldown` en `process_remediation`, pasar `redis_client` desde
  `main._process_alert_with_diagnosis`, tests (adquiere/bloquea/error→escala/None→pasa).
- **Test de F-05**: mock de Redis con `xadd` que lanza → asserts: dedup-key borrada,
  excepción propagada; y variante con delete fallando → warning + propaga igual.
- **F-04**: timeout dedicado Mattermost (`mattermost_timeout≈10s` en config, usarlo en
  `_post_with_retry` en vez de heredar `http_timeout=360s`).
- Después (H1 review): F-03 (chroma vía `asyncio.to_thread`), F-06 (AOF+PVC Redis o
  degradar el claim en docs), F-11 (incident_id desde la ingesta), F-17 (logs+events en
  el enrichment — primer uso nuevo del LLM).
- **Sin cambios**: sigue pendiente lo de la sesión anterior — recuperación de secrets
  (Jay, MM UI), deploy `cb2d1db` (ahora será un commit nuevo con estos fixes), chaos OOM
  del arco completo, `/promote` (~7 bitácoras `promoted: false`).
- Comando de test para Jay (cuando haya código F-01/F-04):
  `python3 -m pytest agent/tests/ -q`
