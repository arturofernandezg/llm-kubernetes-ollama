# AIOps Infrastructure Agent — Contexto para Claude Code

## Resumen del proyecto

Sistema de Remediación Automática en Kubernetes. Detecta alertas de Prometheus, las procesa con un LLM (junto a una base RAG in-cluster con ChromaDB) y notifica a Mattermost, pudiendo auto-remediar fallos (ej. OOMs).
*(Nota: El proyecto original generaba Terraform, la Fase 1 original se conserva intacta sin interrupción).*
TFG/TFM en MasOrange/Telecable. Rol: ingeniero AIOps.

Flujo objetivo: `Prometheus → Alertmanager → FastAPI (Webhook) → LLM + ChromaDB (RAG) → Mattermost → K8s API`

## Documentación detallada

Cada parte del proyecto tiene su propio archivo en `docs/`:

| Archivo | Contenido |
|---|---|
| `docs/01-architecture.md` | Arquitectura, decisiones de diseño, componentes |
| `docs/02-agent-fastapi.md` | Módulos, endpoints, schemas, retry, métricas, logging |
| `docs/03-kubernetes.md` | Cluster GKE, manifiestos, probes, PDB, NetworkPolicy, SecurityContext |
| `docs/04-cicd-cloudbuild.md` | Cloud Build (tests + build), Artifact Registry, versionado |
| `docs/05-terraform-generator.md` | CLI generate_tf.py, módulo tf_generator.py, template, uso |
| `docs/06-testing.md` | 64 tests en 4 ficheros, mocking, errores comunes y soluciones |
| `docs/07-roadmap.md` | Fases del proyecto, TODOs por fase, mejoras completadas |

**Lee el archivo relevante antes de hacer cambios en esa parte del proyecto.**

## Estado actual

- **Fase 1 (Legado)**: Completa (agente modular + Ollama local + Terraform endpoints + K8s base). Se mantienen los archivos sin borrar.
- **Fase 1 (Nueva)**: Pendiente (Prometheus, Alertmanager, webhook custom en FastAPI, integración Mattermost).
- **Fase 2 (RAG)**: Pendiente (ChromaDB in-cluster, ingesta de documentos históricos, pruebas Vertex AI vs local según disponibilidad de red/VPC).
- **Fase 3 (Remediación Autónoma)**: Pendiente (RBAC para k8s updates automáticos basados en sugerencia segura del LLM).

## Stack

Python 3.11 | FastAPI | httpx | Pydantic v2 | Ollama (qwen2.5:1.5b en K8s, tinyllama default en config.py) | GKE | Cloud Build

## Archivos clave

```
agent/main.py           → FastAPI app (endpoints, retry, metrics)
agent/config.py         → Settings (pydantic-settings) + JSON logging
agent/schemas.py        → Modelos Pydantic v2
agent/extraction.py     → 3 estrategias de extracción JSON
agent/validation.py     → Validación de parámetros GCP
agent/tf_generator.py   → Generación de template Terraform
agent/tests/            → 64 tests en 4 ficheros (endpoints, extraction, tf_generator, validation)
generate_tf.py          → CLI generador de .tf (importa de agent/tf_generator.py)
k8s/                    → Manifiestos K8s (incl. networkpolicy.yaml)
cloudbuild.yaml         → Pipeline: tests (gate) + build + push
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot, 2 nodos)
- **Namespace**: arturo-llm-test
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **NO hay Python local en Windows** — tests se ejecutan en GCloud Shell
- **Sin Cloud NAT** — pods no tienen internet, modelos se cargan manualmente

## Convenciones

- Regiones GCP permitidas (convención): europe-west1/2/3/4, europe-southwest1 (nota: validation.py acepta más regiones — gap conocido)
- Labels obligatorios: managed-by, project, environment, created-by
- Tests con mocking de Ollama (no requieren cluster ni LLM)
- Builds con `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`
- Helpers de test en `tests/helpers.py` (no importar desde conftest.py)

## Notas importantes

- NUNCA borrar el PVC `ollama-pvc` — perderías los modelos LLM cargados
- Los docs/ se mantienen actualizados con errores encontrados y soluciones
- El guion original menciona Jira, pero el proyecto usa Slack en su lugar
