# AIOps Infrastructure Agent — Contexto para Claude Code

## ¿Qué es este proyecto?
Agente de IA de ciclo completo que automatiza despliegues de infraestructura GCP.
TFG/TFM en MasOrange (Telecable). Rol: ingeniero AIOps.

Flujo objetivo (zero-touch):
```
Slack → Agente extrae params → genera .tf → PR en GitHub → validación → terraform apply
```

## Estado actual (Fase 1 completa)
- ✅ Agente FastAPI con LLM local (Ollama/tinyllama) en Kubernetes (GKE)
- ✅ Extracción de parámetros GCP desde lenguaje natural
- ✅ Generación de código Terraform (.tf)
- ✅ Tests unitarios y de integración (pytest)
- ✅ Build con Cloud Build → Artifact Registry
- ❌ Slack integration (Fase 2)
- ❌ GitHub PR automation (Fase 2)
- ❌ Validation agent + confidence score (Fase 3)
- ❌ CI/CD terraform plan/apply (Fase 4)

## Stack técnico
| Componente | Tecnología |
|---|---|
| Agente | Python 3.11, FastAPI, httpx, Pydantic v2 |
| LLM | Ollama (tinyllama) en Kubernetes |
| Infra | GKE (namespace: arturo-llm-test) |
| Build | Cloud Build → Artifact Registry (europe-southwest1) |
| IaC | Terraform (módulo corporativo: terraform-modules/gcp-vm) |

## Archivos clave
```
agent/main.py          → FastAPI app (endpoints + lógica de extracción)
agent/tests/test_main.py → Tests (pytest)
generate_tf.py         → CLI: llama al agente y genera .tf
k8s/                   → Manifiestos Kubernetes
cloudbuild.yaml        → Pipeline de build (Cloud Build)
```

## Kubernetes — namespace: arturo-llm-test
- **agent**: Deployment (2 réplicas), ClusterIP :8000
- **ollama**: Deployment (1 réplica), PVC 20Gi ReadWriteOnce, ClusterIP :11434
- **ollama-pdb**: PodDisruptionBudget (minAvailable: 1)

## Endpoints del agente
| Método | Path | Descripción |
|---|---|---|
| GET | /healthz | Liveness probe (siempre 200, sin deps) |
| GET | /readyz | Readiness probe (verifica Ollama + modelo) |
| GET | /health | Health completo (retrocompatibilidad) |
| POST | /extract | Extracción de parámetros desde texto |

## Comandos frecuentes
```bash
# Tests (en GCloud Shell)
cd agent && pip install -r requirements-dev.txt && pytest tests/ -v

# Port-forward al agente
kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test

# Build
gcloud builds submit --config cloudbuild.yaml

# Deploy K8s
kubectl apply -f k8s/

# Probar extracción
python generate_tf.py "Servidor para proyecto web-prod en europe-west1 con e2-standard-4"
```

## IMPORTANTE — Secrets pendientes (Fase 2)
Para Slack y GitHub no hay gestión de secrets aún.
Usar GCP Secret Manager + ExternalSecrets o K8s Secrets.
NO hardcodear tokens en el código ni en los manifiestos.

## Convenciones de nomenclatura MasOrange
- Regiones permitidas: europe-west1/2/3/4, europe-southwest1
- Tipos de instancia: prefijos e2-, n1-, n2-, n2d-, c2-, m1-, t2d-
- Labels obligatorios: managed-by, project, environment, created-by
