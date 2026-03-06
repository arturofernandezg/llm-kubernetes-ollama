# AIOps Agent — Infraestructura LLM en Kubernetes (GKE)

Fase 1 del proyecto de prácticas de ingeniería AIOps. El objetivo final es construir un agente de ciclo completo que automatice el despliegue de infraestructura: desde una petición en lenguaje natural en Slack hasta la ejecución de `terraform apply` mediante un pipeline de CI/CD en GitHub Actions.

Esta fase establece la infraestructura base: una LLM propia corriendo como servicio en Kubernetes, un agente FastAPI que la consume internamente, y un pipeline de Cloud Build para construir y publicar las imágenes del agente.

---

## Arquitectura actual

```
[Cliente / port-forward]
        │
        ▼
  agent-svc:8000         ← Agente FastAPI (extracción de parámetros con LLM)
        │
        ▼
  ollama-svc:11434       ← API de inferencia LLM (tinyllama / phi3:mini)
        │
        ▼
   ollama-pvc            ← Disco persistente 20GB (modelos)

  apache-svc:80          ← Servidor web (validación de red interna)
```

## Arquitectura objetivo

```
[Slack]
   │
   ▼
[FastAPI agent]  ──►  ollama-svc:11434  (extracción de parámetros)
   │
   ▼
[GitHub API]  ──►  crea rama + commit + PR con código Terraform
   │
   ▼
[GitHub Actions]  ──►  terraform plan / terraform apply
```

---

## Estructura del repositorio

```
llm-kubernetes-ollama/
├── k8s/
│   ├── deployment-ollama.yaml
│   ├── service-ollama.yaml
│   ├── pvc-ollama.yaml
│   ├── deployment-apache.yaml
│   ├── service-apache.yaml
│   ├── deployment-agent.yaml
│   └── service-agent.yaml
├── agent/
│   ├── main.py
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── pytest.ini
│   ├── Dockerfile
│   ├── .dockerignore
│   └── tests/
│       ├── __init__.py
│       └── test_main.py
├── cloudbuild.yaml
└── README.md
```

---

## Requisitos

- Clúster GKE con `kubectl` configurado
- `gcloud` CLI autenticado con permisos sobre el proyecto
- Namespace creado: `kubectl create namespace arturo-llm-test`
- Google Cloud Build habilitado en el proyecto GCP
- Repositorio en Artifact Registry: `aiops-agent` en `europe-southwest1`

---

## Despliegue

### 1. Infraestructura base (Ollama)

```bash
kubectl apply -f k8s/pvc-ollama.yaml
kubectl apply -f k8s/service-ollama.yaml
kubectl apply -f k8s/deployment-ollama.yaml
kubectl get pods -n arturo-llm-test
```

### 2. Cargar un modelo

Sin Cloud NAT, los pods no tienen salida a internet. Los modelos se transfieren manualmente desde una instalación local de Ollama:

```bash
kubectl exec -i <pod> -n arturo-llm-test -- bash -c \
  "mkdir -p /root/.ollama/models/blobs && cat > /root/.ollama/models/blobs/<hash>" \
  < ~/.ollama/models/blobs/<hash>
```

### 3. Apache (validación de red interna)

```bash
kubectl apply -f k8s/deployment-apache.yaml
kubectl apply -f k8s/service-apache.yaml
```

### 4. Agente FastAPI

Construir y publicar la imagen con Cloud Build:

```bash
gcloud builds submit --config cloudbuild.yaml
```

Desplegar en el clúster:

```bash
kubectl apply -f k8s/deployment-agent.yaml
kubectl apply -f k8s/service-agent.yaml
```

### 5. Acceso local (desarrollo)

```bash
# Agente FastAPI
kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test

# Ollama
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test

# Apache
kubectl port-forward svc/apache-svc 8080:80 -n arturo-llm-test
```

---

## Tests

Los tests no requieren Kubernetes ni Ollama — Ollama se mockea completamente.

```bash
cd agent
pip install -r requirements-dev.txt
pytest tests/test_main.py -v
```

Estructura de tests:
- `TestExtractJson` — lógica de extracción de JSON (unitarios)
- `TestValidateParams` — validación de parámetros GCP (unitarios)
- `TestHealthEndpoint` — endpoint `/health` con Ollama mockeado
- `TestExtractEndpoint` — endpoint `/extract` con Ollama mockeado, incluyendo casos de error

---

## Variables de entorno del agente

| Variable | Default | Descripción |
|---|---|---|
| `OLLAMA_URL` | `http://ollama-svc:11434/api/generate` | URL del endpoint de generación de Ollama |
| `OLLAMA_TAGS` | `http://ollama-svc:11434/api/tags` | URL para consultar modelos disponibles |
| `OLLAMA_MODEL` | `tinyllama` | Modelo a usar para inferencia |

---

## Modelos disponibles

| Modelo | Tamaño | RAM necesaria | Estado |
|---|---|---|---|
| tinyllama | 637 MB | ~1 GB | Operativo |
| phi3:mini | 2.2 GB | ~3.5 GB | Almacenado (requiere más RAM) |

---

## Notas

- Los nodos son de tipo **spot** (e2-standard-2), lo que reduce costes pero implica que pueden ser reciclados por GCP en cualquier momento. El PVC garantiza que los modelos no se pierden en esos reinicios.
- Sin **Cloud NAT** configurado, los pods no tienen salida a internet. Los modelos deben cargarse manualmente.
- Las imágenes Docker del agente se construyen con **Google Cloud Build** y se almacenan en **Artifact Registry** (`europe-southwest1`).
- El agente se comunica con Ollama usando el DNS interno del clúster (`ollama-svc:11434`), sin exponer el modelo a internet.
- El modelo y la URL de Ollama son configurables via variables de entorno — no hace falta recompilar la imagen para cambiar de modelo.
