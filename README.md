# AIOps Agent — Infraestructura LLM en Kubernetes (GKE)

Agente de IA de ciclo completo que automatiza el despliegue de infraestructura GCP: desde una petición en lenguaje natural hasta la generación de código Terraform y (en fases futuras) la creación automática de Pull Requests y ejecución de pipelines CI/CD.

Proyecto de prácticas de ingeniería AIOps en MasOrange/Telecable (TFG/TFM).

---

## Arquitectura actual (Fase 1)

```
[Cliente / port-forward / generate_tf.py]
        │
        ▼
  agent-svc:8000         ← Agente FastAPI (2 réplicas, probes, shared client)
        │
        ▼
  ollama-svc:11434       ← Ollama (tinyllama, 1 réplica, PVC 20Gi, PDB)
```

## Arquitectura objetivo

```
[Slack] → [FastAPI Agent] → [Ollama] → [GitHub API] → [GitHub Actions] → [terraform apply]
```

---

## Estructura del repositorio

```
llm-kubernetes-ollama/
├── agent/
│   ├── main.py               # API FastAPI (extracción de parámetros)
│   ├── Dockerfile             # Imagen del agente (python:3.11-slim, non-root)
│   ├── requirements.txt       # Dependencias de producción
│   ├── requirements-dev.txt   # Dependencias de test
│   ├── pytest.ini
│   └── tests/
│       ├── __init__.py
│       └── test_main.py       # 40 tests (unitarios + integración)
├── k8s/
│   ├── deployment-agent.yaml  # 2 réplicas, probes /healthz + /readyz
│   ├── deployment-ollama.yaml # 1 réplica, PVC, probes
│   ├── deployment-apache.yaml # Validación de red interna
│   ├── service-agent.yaml     # ClusterIP :8000
│   ├── service-ollama.yaml    # ClusterIP :11434
│   ├── service-apache.yaml    # ClusterIP :80
│   ├── pvc-ollama.yaml        # 20Gi ReadWriteOnce
│   └── pdb-ollama.yaml        # PodDisruptionBudget (minAvailable: 1)
├── docs/                      # Documentación detallada por componente
├── generate_tf.py             # CLI: extrae params + genera .tf
├── cloudbuild.yaml            # Build pipeline (Cloud Build, dual tag)
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
```

### 5. Verificar

```bash
kubectl get pods -n arturo-llm-test
kubectl get pdb -n arturo-llm-test
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
# 40 tests — no requieren K8s ni Ollama (todo mockeado)
```

---

## Endpoints

| Método | Path | Descripción |
|---|---|---|
| GET | `/healthz` | Liveness probe (siempre 200, sin dependencias) |
| GET | `/readyz` | Readiness probe (verifica Ollama + modelo) |
| GET | `/health` | Health completo (retrocompatibilidad) |
| POST | `/extract` | Extracción de parámetros desde lenguaje natural |

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | Endpoint de generación |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | Endpoint de modelos |
| `OLLAMA_MODEL` | `tinyllama` | Modelo a usar |

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
