---
fecha: 2026-07-02
slug: v2-hardening-review-deploy
promoted: true
---

## Objetivo
Sesión "revisa código para hacer v2 primero, luego cluster": review senior del diff completo
`da7aafb..HEAD` (todo lo que aún no corre en cluster: Eje A grounding + Eje B + temp=0/enrich)
antes de buildear, endurecer lo encontrado, y arrancar el despliegue. **Formato microtask
suspendido a petición de Jay** — sesión larga de arco completo aprovechando el modelo.

## Hecho
- **Review v2** (enrichment.py completo + seal/ground + main + rollback_store + P0·2 + k8s):
  veredicto *sólida y desplegable* con 1 hallazgo P1 de contrato, 1 P2, y un cabo suelto de la
  auditoría que la sesión de Eje A no cerró (el approve humano ejecutaba free-text del LLM).
- **6 piezas de hardening implementadas** (+30 tests; suite ~505 → **~535**, verde en el gate
  de Cloud Build con la imagen `cb2d1db`):
  1. **Regla 4.7** (`remediation.py`): `seal_proposed_action` deja marker `target_unresolved=True`
     al anular la PA; `decide_action` escala ANTES de la regla 5. Cierra el P1.
  2. **`proc.kill()` en `_kubectl_json`** (`enrichment.py`): reap del kubectl en timeout/cancel,
     espejo del executor. Cierra el P2 (fuga de subprocess contra API server colgado).
  3. **Síntesis de `new_value`** (parte de la slice C que quedó fuera): si el valor del LLM es
     inusable (falta / imparseable / ≤ current real), el seal lo sustituye por `2×current` del
     snapshot (`_double_limit_value` + `_normalize_new_value`), preservando `model_new_value`.
     Conservador: valor sano del modelo se respeta; overshoot >2× NO se clampa (regla 4.6 escala).
  4. **Unificación humano/auto**: `process_remediation` sintetiza `structured_command` UNA vez y
     lo expone en el result; la escalación estructurada guarda/enseña ese comando (no el free-text);
     el approve captura snapshot pre-patch + **programa rollback** igual que el auto, reusando el
     `incident_id`. "Lo que el humano aprueba es exactamente lo que se ejecuta."
  5. **`aiops_enrichment_total{gathered|workload_unresolved|skipped|error}`** en main + pre-init:
     la etapa de grounding es observable (se verá en cluster si el RBAC/gather funciona).
  6. **Grounding visible en Mattermost**: "Confidence: 100% _(grounded del cluster; el modelo dijo
     65%)_" en diagnosis+escalación, y el fix determinista mostrado en suggest/escalate.
- Ficheros: `remediation.py`, `enrichment.py`, `main.py`; tests en `test_remediation.py` (+18),
  `test_enrichment.py` (+1), `test_endpoints.py` (+11). Verificación estática (py_compile) OK.
- **Cluster (parcial)**: build `cb2d1db` SUCCESS (tests como gate) · RBAC aplicado. Secrets: ver gotcha.
- **Mini-demo HTML modo-libro** del sistema actual: `docs_sesion/2026-07-02-sistema-actual-minidemo.html`.

## Encontrado / gotchas
- **P1 (review)**: el contrato "target fantasma → escala" NO estaba garantizado. Con PA anulada +
  confidence grounded a 1.0 + comandos SAFE + `risk=low`, `decide_action` habría **AUTO-ejecutado
  los describes/logs del LLM** reportando "remediado" sin arreglar nada; con risk=medium/high
  escalaba. El outcome dependía del risk autoevaluado del 1.5b — justo lo que v2 elimina. Y la
  rama fantasma es EXACTAMENTE lo que pasa si el RBAC nuevo no está aplicado (Forbidden en el
  `get replicaset` → workload sin resolver en todas las alertas).
- **P2 (review)**: `_kubectl_json` decía en su docstring "same safe invocation as executor" pero
  NO mataba el proceso en timeout (el executor sí: kill + communicate). Hasta 3 kubectl huérfanos
  por alerta con API server colgado.
- **Cabo de la auditoría**: la fila Eje A decía "unifica el comando humano/auto" y la sesión de
  ayer no lo tocó — el approve ejecutaba `incident.safe_commands` (free-text LLM) sin snapshot ni
  rollback. La escalación con botones además exigía `safe_commands` no vacío (un estructurado con
  todos los comandos filtrados no ofrecía botones).
- **INCIDENTE secrets** ⚠️: Jay ejecutó `secrets-setup.sh` sin editar los placeholders → el script
  **sobreescribió** `mattermost-webhook/url` real con `<TU-TOKEN-AQUI>` (dijo `configured`, no
  `created`) y creó `mm-command-token` con el placeholder literal. Recuperación: el pod vivo
  (imagen vieja, arrancado pre-pisotón) conserva la URL real en su env →
  `kubectl exec deploy/agent -- printenv MATTERMOST_WEBHOOK_URL` **antes** de reiniciar nada;
  el token del slash está en MM UI → Integrations → Slash Commands. Lección: los scripts-plantilla
  con placeholders DEBEN abortar si detectan valores sin rellenar (guard pendiente).
- `deployment-agent.yaml`: `mattermost-webhook/url` NO es `optional`; las claves de `agent-secrets`
  sí (`optional: true`) — por eso el pod viejo corría sin ellas (y por eso el HMAC estaba
  desactivado en la práctica hasta P0·2).
- El warning de Cloud Build (`logging.logWriter`) es el benigno conocido (backlog).

## Decisiones + por qué
- **Regla 4.7 explícita en la cascada** (vs. "no groundear confidence al dropear"): robusta ante
  cualquier combinación risk/confidence del modelo; el marker solo lo escribe el seal → blast-radius
  mínimo; colocada ANTES de la regla 5 para que el outcome no dependa del risk del modelo.
- **Síntesis 2× conservadora**: solo cuando el valor del LLM no puede gobernar una subida válida.
  El overshoot >2× se mantiene como ESCALATE — reescribirlo en silencio bypasearía la semántica
  de una regla acordada con el tutor (escalar = ojos humanos, no corrección muda).
- **`structured_command` sintetizado UNA vez en `process_remediation`** → fuente única para el
  camino auto Y el humano; la escalación almacena `[structured_command]` → what-you-approve-is-what-runs,
  y visible en el attachment de Mattermost ("Comando determinista del motor").
- **Rollback en approve espeja la condición del auto** (success + enabled + snapshot) y reusa el
  `incident_id` de la escalación → trazabilidad E2E de un mismo incidente.
- **Métrica de enrichment en `main`** (no en enrichment.py): main ve el outcome completo
  (gathered vs workload_unresolved vs skipped vs error) sin acoplar el módulo puro a prometheus.
- Formato microtask suspendido esta sesión por decisión explícita de Jay ("saquemos provecho del
  modelo"): review + 6 fixes + tests en un solo arco. Las reglas no negociables (no pytest/git por
  mi cuenta, one-liners para Jay) siguieron vigentes.

## Siguiente
- **Jay, recuperación de secrets (en orden, ANTES de tocar el deployment)**:
  1. `kubectl exec -n arturo-llm-test deploy/agent -- printenv MATTERMOST_WEBHOOK_URL`
  2. restaurar `mattermost-webhook` con esa URL (create secret + apply)
  3. token del slash desde MM UI → recrear `agent-secrets` (webhook-secret nuevo + mm-command-token real)
  4. verificar con `kubectl get secret ... | base64 -d` que ya no hay placeholder.
- Luego: `kubectl apply -f k8s/prometheus.yaml` + rollout restart prometheus + `set image` a `cb2d1db`.
- **Validación chaos OOM** del arco completo: log `seal_proposed_action: sealed from cluster snapshot`,
  `aiops_enrichment_total{outcome="gathered"}` subiendo, auto SIN `NotFound`, rollback solo si no cura,
  mensaje Mattermost con confidence grounded.
- **Guard anti-placeholder en `secrets-setup.sh`** (ofrecido, pendiente de OK).
- Panel Grafana para `aiops_enrichment_total` (sesión Gate 8 / screenshots).
- `/promote`: hay ~6 bitácoras `promoted: false` y el roadmap está desactualizado (dice "Eje A
  siguiente" y "staged sin commitear" cuando ya está commiteado y buildeado en `cb2d1db`).
