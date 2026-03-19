# AIOps Agent — Sistema de Remediación Automática en Kubernetes (GKE)

Proyecto que evoluciona hacia una plataforma AIOps de remediación automática. Basado en alertas de Prometheus (ej. OOM), el agente interactúa con Mattermost y utiliza Modelos de Lenguaje (LLM) y bases de datos vectoriales (RAG con ChromaDB) para sugerir o ejecutar remediaciones en el cluster (ej. aumentar memoria, reiniciar pods). (Anteriormente: agente de despliegue de infraestructura GCP mediante Terraform).

Proyecto de prácticas de ingeniería AIOps en MasOrange/Telecable (TFG/TFM).

---

## Arquitectura actual (Fase 1 - Legado)

```
[Cliente / port-forward / generate_tf.py]
        │
        ▼
  agent-svc:8000         ← Agente FastAPI (1 réplica, probes, retry, metrics)
        │
        ▼
  ollama-svc:11434       ← Ollama (qwen2.5:1.5b, 1 réplica, PVC 20Gi, PDB)
```

## Arquitectura objetivo (Sistema de Remediación)

```
[Prometheus / Alertmanager] → [FastAPI Webhook] → [(RAG) ChromaDB + LLM (Vertex/Ollama)] → [Mattermost] → [K8s API Server (Remediación)]
```

---

## Estructura del repositorio

```
llm-kubernetes-ollama/
├── agent/
│   ├── main.py               # FastAPI app (endpoints, retry, metrics)
│   ├── config.py             # Settings (pydantic-settings) + JSON logging
│   ├── schemas.py            # Modelos Pydantic v2
│   ├── extraction.py         # 3 estrategias de extracción JSON del LLM
│   ├── validation.py         # Validación de parámetros GCP
│   ├── tf_generator.py       # Generación de template Terraform
│   ├── Dockerfile             # Imagen del agente (python:3.11-slim, non-root)
│   ├── requirements.txt       # Dependencias de producción
│   ├── requirements-dev.txt   # Dependencias de test
│   ├── pytest.ini
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py        # Fixtures (api_client)
│       ├── helpers.py         # Mock helpers y datos de test
│       ├── test_endpoints.py  # 26 tests (health, extract, retry, metrics)
│       ├── test_extraction.py # 11 tests (JSON extraction strategies)
│       ├── test_tf_generator.py # 16 tests (safe_name, template)
│       └── test_validation.py # 6 tests (GCP parameter validation)
├── k8s/
│   ├── deployment-agent.yaml  # 1 réplica, probes, securityContext
│   ├── deployment-ollama.yaml # 1 réplica, PVC, probes
│   ├── deployment-apache.yaml # Validación de red interna
│   ├── service-agent.yaml     # ClusterIP :8000
│   ├── service-ollama.yaml    # ClusterIP :11434
│   ├── service-apache.yaml    # ClusterIP :80
│   ├── pvc-ollama.yaml        # 20Gi ReadWriteOnce
│   ├── pdb-ollama.yaml        # PodDisruptionBudget (minAvailable: 1)
│   └── networkpolicy.yaml     # Segmentación de tráfico entre pods
├── docs/                      # Documentación detallada por componente
├── generate_tf.py             # CLI: extrae params + genera .tf
├── cloudbuild.yaml            # Build pipeline (tests + build + push)
├── CLAUDE.md                  # Contexto para Claude Code
└── README.md
```

---

## Requisitos

- Cluster GKE con `kubectl` configurado
- `gcloud` CLI autenticado con permisos sobre el proyecto `uniovi-ai-infra-agent`
- Namespace: `arturo-llm-test`
- Google Cloud Build habilitado
- Artifact Registry: `aiops-agent` en `europe-southwest1`

---

## Despliegue

### 1. Conectar al cluster

```bash
gcloud container clusters get-credentials ai-infra-agent \
  --zone europe-southwest1-a --project uniovi-ai-infra-agent
```

### 2. Infraestructura base (Ollama)

```bash
kubectl apply -f k8s/pvc-ollama.yaml
kubectl apply -f k8s/pdb-ollama.yaml
kubectl apply -f k8s/service-ollama.yaml
kubectl apply -f k8s/deployment-ollama.yaml
```

### 3. Cargar modelo (sin Cloud NAT)

```bash
kubectl exec -i <pod-ollama> -n arturo-llm-test -- bash -c \
  "mkdir -p /root/.ollama/models/blobs && cat > /root/.ollama/models/blobs/<hash>" \
  < ~/.ollama/models/blobs/<hash>
```

### 4. Build y deploy del agente

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)

kubectl apply -f k8s/deployment-agent.yaml
kubectl apply -f k8s/service-agent.yaml
kubectl apply -f k8s/networkpolicy.yaml
```

### 5. Verificar

```bash
kubectl get pods -n arturo-llm-test
kubectl get pdb -n arturo-llm-test
kubectl get networkpolicy -n arturo-llm-test
```

---

## Uso

```bash
# Port-forward al agente
kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test

# Health checks
curl http://localhost:8000/healthz     # Liveness (siempre 200)
curl http://localhost:8000/readyz      # Readiness (verifica Ollama)

# Extraer parámetros
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"message": "Servidor para web-prod en europe-west1 con e2-standard-4"}'

# Métricas Prometheus
curl http://localhost:8000/metrics

# Generar Terraform
python generate_tf.py "Servidor para web-prod en europe-west1 con e2-standard-4"
python generate_tf.py --dry-run "Solo quiero ver qué parámetros extrae"
```

---

## Tests

```bash
cd agent
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
# 59 tests — no requieren K8s ni Ollama (todo mockeado)
```

---

## Endpoints

| Método | Path | Descripción |
|---|---|---|
| GET | `/healthz` | Liveness probe (siempre 200, sin dependencias) |
| GET | `/readyz` | Readiness probe (verifica Ollama + modelo) |
| GET | `/health` | Redirect 307 a /readyz (retrocompatibilidad) |
| POST | `/extract` | Extracción de parámetros desde lenguaje natural |
| GET | `/metrics` | Métricas Prometheus (request count, latency, custom counters) |

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint de generación |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint de modelos |
| `OLLAMA_MODEL` | `tinyllama` | Modelo a usar (en K8s: `qwen2.5:1.5b`) |
| `HTTP_TIMEOUT` | `120.0` | Timeout del cliente HTTP (segundos) |
| `HEALTH_TIMEOUT` | `5.0` | Timeout para health checks (segundos) |
| `RETRY_MAX_ATTEMPTS` | `3` | Intentos máximos de retry |
| `RETRY_BASE_DELAY` | `1.0` | Delay base para backoff exponencial |
| `RETRY_MAX_DELAY` | `10.0` | Delay máximo entre reintentos |
| `LOG_LEVEL` | `INFO` | Nivel de logging |

---

## Documentación

Ver `docs/` para documentación detallada de cada componente:
arquitectura, agente, K8s, CI/CD, Terraform, testing, y roadmap.

---

## Notas

- Nodos **spot** (e2-standard-2): reduce costes, pero pueden ser reciclados. Mitigado con PVC + PDB.
- Sin **Cloud NAT**: los pods no tienen internet. Modelos se cargan manualmente.
- Imágenes tagueadas con **commit SHA** + `:latest` para rollback.
- Comunicación interna via **DNS del cluster** (ClusterIP), nada expuesto a internet.
- **SecurityContext**: agent corre como non-root con filesystem read-only.
- **NetworkPolicy**: tráfico entre pods restringido (Ollama ← agent, agent ← Apache).
