# AIOps Agent — Infraestructura LLM en Kubernetes (GKE)

Fase 1 del proyecto de prácticas de ingeniería AIOps. El objetivo final es construir un agente de ciclo completo que automatice el despliegue de infraestructura: desde una petición en lenguaje natural en Slack hasta la ejecución de `terraform apply` mediante un pipeline de CI/CD en GitHub Actions.

Esta fase establece la infraestructura base: una LLM propia corriendo como servicio en Kubernetes, accesible internamente por cualquier componente del agente.

---

## Arquitectura actual

```
[Cliente / port-forward]
        │
        ▼
  apache-svc:80          ← Servidor web (validación de red interna)
        │
        ▼
  ollama-svc:11434       ← API de inferencia LLM (tinyllama / phi3:mini)
        │
        ▼
   ollama-pvc            ← Disco persistente 20GB (modelos)
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
│   └── service-apache.yaml
├── agent/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
└── README.md
```

---

## Requisitos

- Clúster GKE con `kubectl` configurado
- `gcloud` CLI autenticado con permisos sobre el proyecto
- Namespace creado: `kubectl create namespace arturo-llm-test`
- Google Cloud Build habilitado en el proyecto GCP (para construir imágenes sin Docker local)

---

## Despliegue

### 1. Infraestructura base (Ollama)

```bash
kubectl apply -f k8s/pvc-ollama.yaml
kubectl apply -f k8s/service-ollama.yaml
kubectl apply -f k8s/deployment-ollama.yaml
```

Verificar que el pod está corriendo:

```bash
kubectl get pods -n arturo-llm-test
```

### 2. Cargar un modelo

Si no hay acceso a internet desde los pods (sin Cloud NAT), los modelos se transfieren manualmente desde una instalación local de Ollama:

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

### 4. Acceso local (desarrollo)

```bash
# Ollama
kubectl port-forward svc/ollama-svc 11434:11434 -n arturo-llm-test

# Apache
kubectl port-forward svc/apache-svc 8080:80 -n arturo-llm-test
```

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
- Las imágenes Docker del agente se construyen con **Google Cloud Build** y se almacenan en Google Container Registry (GCR).