# AIOps Agent — Remediación Automática en Kubernetes (GKE)

Sistema AIOps de detección y remediación de incidencias en Kubernetes. Detecta alertas de Prometheus, las enriquece con un pipeline RAG (ChromaDB + Ollama), genera un diagnóstico estructurado y notifica a Mattermost. Puede ejecutar remediaciones automáticas (ej. aumentar memory limits ante OOMKilled) según umbrales configurables.

Proyecto TFG/TFM en MasOrange/Telecable.

---

## Pipeline actual

```
Prometheus → Alertmanager → FastAPI /webhook/alert → RAG (ChromaDB + nomic-embed-text)
                                                    → LLM (Ollama qwen2.5:1.5b)
                                                    → Mattermost (ChatOps)
                                                    → K8s API (remediación, modo dry-run por defecto)
```

**Reglas de alerta activas** (Prometheus, `arturo-monitoring`):
- `KubePodOOMKilled` — container terminado por OOM killer
- `KubePodCrashLoopBackOff` — >3 reinicios en 15 min
- `HighMemory` — uso >90% memory limit durante 5 min
- `HighCPU` — uso >90% CPU limit durante 5 min
- `TargetDown` — endpoint Prometheus sin respuesta

---

## Namespaces

| Namespace | Componentes |
|---|---|
| `arturo-llm-test` | agent, ollama, chromadb |
| `arturo-monitoring` | prometheus, alertmanager, kube-state-metrics, grafana |
| `arturo-mattermost` | mattermost, postgres |

---

## Estructura del repositorio

```
llm-kubernetes-ollama/
├── agent/
│   ├── main.py               # FastAPI app (webhook, métricas, pipeline RAG+LLM)
│   ├── config.py             # Settings (pydantic-settings) + JSON logging
│   ├── schemas.py            # Modelos Pydantic v2 (AlertmanagerPayload, AlertItem…)
│   ├── extraction.py         # 3 estrategias de extracción JSON del LLM
│   ├── validation.py         # Validación de parámetros GCP
│   ├── mattermost.py         # Cliente HTTP async Mattermost con retry/backoff
│   ├── rag.py                # Cliente ChromaDB, ingesta runbooks, query semántica
│   ├── diagnosis.py          # Prompt AIOps contextual, generate_diagnosis(), JSON
│   ├── remediation.py        # Validation layer, motor de decisión, executor kubectl
│   ├── tf_generator.py       # Generación Terraform (Fase 0 legacy)
│   ├── runbooks/             # 16 runbooks YAML semilla (OOMKilled, CrashLoop…)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── tests/                # 124 tests en 7 ficheros (no requieren K8s ni Ollama)
├── k8s/
│   ├── deployment-agent.yaml     # Agent: 1 réplica, probes, securityContext
│   ├── deployment-ollama.yaml    # Ollama: 1 réplica, PVC 20Gi, PDB
│   ├── deployment-apache.yaml    # Apache: validación de red interna
│   ├── service-agent.yaml        # ClusterIP :8000 (annotations scrape Prometheus)
│   ├── service-ollama.yaml       # ClusterIP :11434
│   ├── service-apache.yaml       # ClusterIP :80
│   ├── pvc-ollama.yaml           # 20Gi ReadWriteOnce (NUNCA borrar)
│   ├── pdb-ollama.yaml           # PodDisruptionBudget (minAvailable: 1)
│   ├── networkpolicy.yaml        # Segmentación de tráfico entre namespaces
│   ├── prometheus.yaml           # Prometheus + kube-state-metrics + 5 reglas
│   ├── alertmanager.yaml         # Alertmanager → agent-svc:8000/webhook/alert
│   ├── chromadb.yaml             # ChromaDB StatefulSet + PVC
│   ├── mattermost.yaml           # Mattermost + PostgreSQL StatefulSet
│   ├── grafana.yaml              # Grafana stateless + datasource + dashboard + contact point
│   └── rbac.yaml                 # Role + RoleBinding para remediación (patch pods)
├── docs/                         # Documentación detallada por componente
├── docs_sesion/                  # Diarios de sesión de desarrollo
├── generate_tf.py                # CLI legacy: extrae params + genera .tf
├── cloudbuild.yaml               # Pipeline Cloud Build (tests → build → push AR)
├── CLAUDE.md                     # Contexto para Claude Code
└── README.md
```

---

## Requisitos

- GKE cluster `ai-infra-agent` (europe-southwest1-a, e2-standard-2 spot, 2 nodos)
- `gcloud` CLI autenticado con permisos sobre `uniovi-ai-infra-agent`
- `kubectl` configurado
- Artifact Registry: `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent`
- **Sin Cloud NAT**: los pods no tienen internet. Imágenes externas (ej. KSM) se copian a AR con `crane`.

---

## Despliegue

### 1. Conectar al cluster

```
gcloud container clusters get-credentials ai-infra-agent --zone europe-southwest1-a --project uniovi-ai-infra-agent
```

### 2. Namespace core (arturo-llm-test)

```
kubectl apply -f k8s/pvc-ollama.yaml
kubectl apply -f k8s/pdb-ollama.yaml
kubectl apply -f k8s/service-ollama.yaml
kubectl apply -f k8s/deployment-ollama.yaml
kubectl apply -f k8s/service-agent.yaml
kubectl apply -f k8s/deployment-agent.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/rbac.yaml
```

### 3. Cargar modelos en Ollama (sin Cloud NAT, se cargan manualmente)

```
kubectl exec -it <pod-ollama> -n arturo-llm-test -- ollama pull qwen2.5:1.5b
kubectl exec -it <pod-ollama> -n arturo-llm-test -- ollama pull nomic-embed-text
```

### 4. ChromaDB

```
kubectl apply -f k8s/chromadb.yaml
```

### 5. Observabilidad (arturo-monitoring)

```
kubectl apply -f k8s/prometheus.yaml
kubectl apply -f k8s/alertmanager.yaml
```

### 6. Grafana (arturo-monitoring)

Primero crear el Secret (nada sensible en el repo):
```
kubectl create secret generic grafana-admin -n arturo-monitoring --from-literal=admin-user=admin --from-literal=admin-password='<pass>'
```
```
kubectl apply -f k8s/grafana.yaml
```

### 7. ChatOps (arturo-mattermost)

```
kubectl apply -f k8s/mattermost.yaml
```

### 8. Build y despliegue del agente

```
gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)
kubectl rollout restart deploy/agent -n arturo-llm-test
```

### 9. Verificar

```
kubectl get pods -n arturo-llm-test
kubectl get pods -n arturo-monitoring
kubectl get pods -n arturo-mattermost
```

---

## Acceso a UIs (port-forward)

Cada comando en su propia terminal:

```
kubectl port-forward svc/grafana-svc 3000:3000 -n arturo-monitoring
kubectl port-forward svc/prometheus-svc 9090:9090 -n arturo-monitoring
kubectl port-forward svc/alertmanager-svc 9093:9093 -n arturo-monitoring
kubectl port-forward svc/mattermost-svc 8065:8065 -n arturo-mattermost
```

| UI | URL | Credenciales |
|---|---|---|
| Grafana | http://localhost:3000 | Secret `grafana-admin` |
| Prometheus | http://localhost:9090/targets | — |
| Alertmanager | http://localhost:9093 | — |
| Mattermost | http://localhost:8065 | Usuario configurado en setup |

---

## Demo E2E — Pipeline completo

Arranca los port-forwards de arriba, luego:

### 1. Verificar que los pods están Running

```
kubectl get pods -n arturo-llm-test && kubectl get pods -n arturo-monitoring && kubectl get pods -n arturo-mattermost
```

### 2. Verificar targets Prometheus UP

Abrir http://localhost:9090/targets — deben aparecer `agent-svc:8000` y `kube-state-metrics-svc:8080` en estado **UP**.

### 3. Disparar alerta de prueba

```
curl -X POST http://localhost:9093/api/v2/alerts -H "Content-Type: application/json" -d '[{"labels":{"alertname":"KubePodOOMKilled","pod":"test-pod","namespace":"arturo-llm-test","severity":"critical"},"annotations":{"summary":"Test OOM","description":"Container test was killed by OOM killer."},"startsAt":"2026-04-22T10:00:00Z"}]'
```

### 4. Observar el pipeline

```
kubectl logs -n arturo-llm-test deploy/agent --tail=80 -f
```

Secuencia esperada en los logs:
1. `POST /webhook/alert` recibido
2. RAG query a ChromaDB (runbook recuperado)
3. LLM genera diagnóstico JSON (`diagnosis`, `commands`, `confidence`, `risk`)
4. Remediación evaluada (dry-run por defecto)
5. Notificación enviada a Mattermost

### 5. Comprobar métricas en Grafana

Abrir http://localhost:3000 → Dashboard **"AIOps Agent — Overview"**:
- Panel *Diagnoses / s* → spike en `success`
- Panel *Webhook latency p95 / p50* → latencia del procesamiento
- Panel *Scrape targets* → ambos en verde

### 6. Comprobar notificación en Mattermost

Abrir http://localhost:8065 → canal configurado con el incoming webhook → debe aparecer el mensaje con diagnóstico y comandos sugeridos.

---

## Endpoints del agente

| Método | Path | Descripción |
|---|---|---|
| GET | `/healthz` | Liveness probe (siempre 200) |
| GET | `/readyz` | Readiness probe (verifica Ollama + modelo) |
| POST | `/webhook/alert` | Recibe payload Alertmanager, dispara pipeline RAG+LLM |
| GET | `/metrics` | Métricas Prometheus (counters aiops_* + auto-instrumentator) |
| POST | `/extract` | Extracción de parámetros GCP (Fase 0 legacy) |

**Métricas custom** (`/metrics`):
- `aiops_diagnosis_total{outcome}` — diagnósticos por resultado (success, rag_ok, rag_failed, llm_failed, pipeline_failed)
- `aiops_remediation_total{action}` — remediaciones por acción (auto_remediate, escalate, suggest_only, skipped)
- `aiops_feedback_total{outcome}` — persistencia en ChromaDB (persisted, skipped, failed)
- `aiops_ollama_retries_total{outcome}` — retries Ollama (success, exhausted)
- `aiops_extraction_total{method}` — método de extracción JSON (direct, markdown_block, regex_search, failed)

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint generación LLM |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint listado de modelos |
| `OLLAMA_MODEL` | `tinyllama` | Modelo generación (en K8s: `qwen2.5:1.5b`) |
| `OLLAMA_EMBED_URL` | `http://ollama-svc:11434/api/embeddings` | Endpoint embeddings |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings |
| `CHROMADB_HOST` | `chromadb-svc` | Host ChromaDB |
| `CHROMADB_PORT` | `8000` | Puerto ChromaDB |
| `MATTERMOST_WEBHOOK_URL` | `None` | URL webhook entrante Mattermost (obligatorio en K8s) |
| `REMEDIATION_ENABLED` | `false` | Activar remediación autónoma |
| `REMEDIATION_DRY_RUN` | `true` | Dry-run (no ejecuta kubectl real) |
| `REMEDIATION_AUTO_CONFIDENCE` | `0.8` | Umbral mínimo de confianza para auto-remediar |
| `REMEDIATION_AUTO_MAX_RISK` | `low` | Riesgo máximo para auto-remediar |
| `REMEDIATION_COMMAND_TIMEOUT` | `30` | Timeout por comando kubectl (segundos) |
| `HTTP_TIMEOUT` | `120.0` | Timeout cliente HTTP |
| `HEALTH_TIMEOUT` | `5.0` | Timeout health checks |
| `RETRY_MAX_ATTEMPTS` | `3` | Intentos máximos retry Ollama |
| `RETRY_BASE_DELAY` | `1.0` | Delay base backoff exponencial (segundos) |
| `RETRY_MAX_DELAY` | `10.0` | Delay máximo entre reintentos (segundos) |
| `LOG_LEVEL` | `INFO` | Nivel de logging |

---

## Tests

```
cd agent && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest tests/ -v
```

124 tests en 7 ficheros. No requieren K8s, Ollama ni ChromaDB (todo mockeado).

---

## Notas

- **Sin Cloud NAT**: pods sin internet. Imágenes de `registry.k8s.io` se copian a AR con `crane copy --platform linux/amd64`.
- **Nodos spot** (e2-standard-2): pueden reciclarse. PVC + PDB mitigan la pérdida de datos.
- **NUNCA borrar `ollama-pvc`**: contiene los modelos LLM cargados manualmente.
- **Imágenes en AR**: `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/<nombre>:<tag>`
- **SecurityContext**: agent corre como non-root con filesystem read-only.
- **Prometheus scrape**: `scrape_interval: 30s`, `scrape_timeout: 20s`. KSM en AR (mirror de `registry.k8s.io`).
