# Sesión 2026-04-24 — Fase 3: Activación del motor de remediación (dry-run)

## Objetivo

Activar el motor de remediación en modo dry-run para verificar que el árbol de decisión
(`auto_remediate` / `escalate` / `suggest_only`) responde correctamente. Hasta esta sesión
toda alerta caía en `skipped` porque `REMEDIATION_ENABLED=false` por defecto.

## Decisiones tomadas

| Aspecto | Elección | Motivo |
|---|---|---|
| Modo inicial | `DRY_RUN=true` | No ejecutar `kubectl` reales hasta acuerdo con tutor |
| Activación | Env vars en `deployment-agent.yaml` | Sin rebuild de imagen — config.py lee desde env |
| RBAC | `k8s/rbac.yaml` sin cambios | Ya estaba escrito con permisos least-privilege correctos |

## Archivos modificados

| Ruta | Cambio |
|---|---|
| `k8s/deployment-agent.yaml` | Añadidos `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=true` en bloque env |
| `docs/07-roadmap.md` | RBAC y flags marcados ✅, E2E anotado |
| `CLAUDE.md` | Fase 3 actualizada de "Pendiente" a "En curso (dry-run activo)" |

## Comandos ejecutados

```
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/deployment-agent.yaml
kubectl rollout status deployment/agent -n arturo-llm-test
kubectl port-forward -n arturo-monitoring svc/alertmanager-svc 9093:9093
curl -X POST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"KubePodOOMKilled","pod":"test-pod","namespace":"arturo-llm-test","severity":"critical"},"annotations":{"summary":"Test OOM","description":"Test."},"startsAt":"2026-04-23T10:00:00Z"}]'
kubectl logs -n arturo-llm-test deployment/agent --tail=80
```

## Resultado E2E verificado

```
07:28:32  RAG retrieval: 3 runbooks, 2 incidents for query: KubePodOOMKilled...
07:32:03  Diagnosis generated: confidence=0.90 risk=high duration=211119ms
07:32:03  Remediation decision: action=escalate | risk=high | confidence=0.90 | commands_total=3 | blocked=0
07:32:04  Persisted incident incident-KubePodOOMKilled-1777015923 (outcome=escalate)
07:32:06  Successfully sent alert to Mattermost (Attempt 1)
```

- Motor activo — `action=escalate`, no `skipped`.
- Árbol de decisión correcto: `confidence=0.90 ≥ 0.8` cumple umbral pero `risk=high` bloquea
  `auto_remediate` y escala a humano (regla 3 de `remediation.py:133-141`).
- Incidente persistido en colección `incidents` con `outcome=escalate` (feedback loop activo).
- LLM tardó 211s (dentro de `HTTP_TIMEOUT=240`).

## Próxima sesión

- Forzar un escenario `risk=low + confidence≥0.8` para observar `auto_remediate` con prefijo `[DRY-RUN]` en logs.
- Valorar con tutor si pasar a `REMEDIATION_DRY_RUN=false` para ejecución real.
- Webhook entrante de Mattermost (cierre pendiente de Fase 1).

## Vault Impact

| Área | Archivo vault | Cambio |
|---|---|---|
| AIOps project node | `01_Projects/AIOps_K8s_Agent.md` | Fase 3 activa en dry-run. Motor escalate verificado E2E (2026-04-24). |
