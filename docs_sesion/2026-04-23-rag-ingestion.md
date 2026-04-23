# Sesión 2026-04-23 — Fase 2: Ingesta de runbooks en ChromaDB

## Objetivo

Ingestar los 16 runbooks semilla en ChromaDB para activar el pipeline RAG end-to-end.
Hasta ahora cada alerta recibe diagnóstico zero-shot (ChromaDB vacío). Esta sesión cierra Fase 2.

## Decisiones tomadas

| Aspecto | Elección | Motivo |
|---|---|---|
| Mecanismo de ingesta | K8s Job (`job-ingest-runbooks.yaml`) | Reproducible, checked-in, observable. No `kubectl exec` manual. |
| Entrypoint | `agent/ingest_runbooks.py` nuevo CLI | Patrón análogo a `generate_tf.py`. Reutiliza `rag.ingest_all_runbooks` — 0 lógica duplicada. |
| Etiqueta del Job pod | `app: agent` | Hereda reglas egress de `networkpolicy.yaml` hacia ChromaDB + Ollama sin cambios. |
| Idempotencia | `collection.upsert` en `rag.py:99` | Re-ejecutar el Job es seguro, no duplica documentos. |
| Persistencia | PVC existente de ChromaDB | Datos sobreviven a reinicios de nodo spot. |

## Archivos creados/modificados

| Ruta | Cambio |
|---|---|
| `agent/ingest_runbooks.py` | NUEVO — CLI async wrapper, ~40 líneas |
| `agent/tests/test_ingest_runbooks.py` | NUEVO — 3 tests con mocking |
| `k8s/job-ingest-runbooks.yaml` | NUEVO — Job en `arturo-llm-test` |
| `docs/07-roadmap.md` | Runbooks semilla marcados ✅, Fase 2 a ~99% |
| `CLAUDE.md` | Estado Fase 2 actualizado |

## Comandos de despliegue

### 1. Build y push (incluye los 3 tests nuevos en la suite)

```
gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)
```

### 2. Ejecutar el Job de ingesta

```
kubectl apply -f k8s/job-ingest-runbooks.yaml
kubectl wait --for=condition=complete job/runbooks-ingest -n arturo-llm-test --timeout=180s
kubectl logs -n arturo-llm-test job/runbooks-ingest
```

Salida esperada en los logs:
```
Ingesting runbooks from /app/runbooks ...
  ChromaDB : chromadb-svc:8000
  Embeddings model : nomic-embed-text @ http://ollama-svc:11434/api/embeddings
Done — ingested: 16, errors: 0, total: 16
```

### 3. Verificar colección en ChromaDB

```
kubectl port-forward -n arturo-llm-test svc/chromadb-svc 8001:8000
curl -s http://localhost:8001/api/v1/collections | jq '.[] | {name}'
```

Esperar colección `runbooks_v1` en la lista.

### 4. E2E con alerta enriquecida

```
kubectl port-forward -n arturo-monitoring svc/alertmanager-svc 9093:9093
curl -X POST http://localhost:9093/api/v2/alerts -H "Content-Type: application/json" -d '[{"labels":{"alertname":"KubePodOOMKilled","pod":"test-pod","namespace":"arturo-llm-test","severity":"critical"},"annotations":{"summary":"Test OOM","description":"Test."},"startsAt":"2026-04-23T10:00:00Z"}]'
```

Verificar:
- Agent logs: `"rag_ok": true`, documentos recuperados > 0.
- Métrica `aiops_diagnosis_total{rag_ok="true",success="true"}` incrementa.
- Mensaje en Mattermost incluye `diagnosis`, `commands`, `confidence`, `risk`, `explanation` con contenido real del runbook OOMKilled.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| `nomic-embed-text` no cargado en Ollama | Verificar: `curl $OLLAMA_TAGS` desde el pod agente. Estado actual: cargado (CLAUDE.md). |
| Job falla por ChromaDB no alcanzable | `backoffLimit: 2`, logs del Job muestran el error. Verificar NetworkPolicy. |
| Job ya existe de un run anterior | `kubectl delete job runbooks-ingest -n arturo-llm-test` antes de re-aplicar (o usar `ttlSecondsAfterFinished: 300`). |

## Estado final

⏳ Pendiente de aplicar en cluster.

## Próxima sesión (Fase 3)

- `kubectl apply -f k8s/rbac.yaml` — RBAC de remediación.
- Añadir `REMEDIATION_ENABLED=true`, `REMEDIATION_DRY_RUN=true` en `deployment-agent.yaml`.
- Disparar alerta OOMKilled real → verificar decisión `suggest_only` o `auto_remediate` (dry-run) en logs + Mattermost.

## Vault Impact

| Área | Archivo vault | Cambio |
|---|---|---|
| AIOps project node | `01_Projects/AIOps_K8s_Agent.md` | Fase 2 cerrada: CLI ingesta + Job + 16 runbooks activos en ChromaDB. Pipeline RAG end-to-end operativo. |
