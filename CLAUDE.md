# AIOps Infrastructure Agent — Contexto para Claude Code

## Resumen del proyecto

Agente de IA de ciclo completo para automatizar despliegues de infraestructura GCP.
TFG/TFM en MasOrange/Telecable. Rol: ingeniero AIOps.

Flujo objetivo: `Slack → Agente → genera .tf → PR en GitHub → validación → terraform apply`

## Documentación detallada

Cada parte del proyecto tiene su propio archivo en `docs/`:

| Archivo | Contenido |
|---|---|
| `docs/01-architecture.md` | Arquitectura, decisiones de diseño, componentes |
| `docs/02-agent-fastapi.md` | Endpoints, schemas, flujo de extracción, manejo de errores |
| `docs/03-kubernetes.md` | Cluster GKE, manifiestos, probes, PDB, comandos |
| `docs/04-cicd-cloudbuild.md` | Cloud Build, Artifact Registry, versionado |
| `docs/05-terraform-generator.md` | CLI generate_tf.py, template, uso |
| `docs/06-testing.md` | Tests, mocking, errores comunes y soluciones |
| `docs/07-roadmap.md` | Fases del proyecto, TODOs por fase |

**Lee el archivo relevante antes de hacer cambios en esa parte del proyecto.**

## Estado actual

- **Fase 1**: Completa (agente + Ollama + tests + build + K8s)
- **Fase 2**: Pendiente (Slack + GitHub PRs)
- **Fase 3**: Pendiente (validación + score confianza)
- **Fase 4**: Pendiente (CI/CD terraform)

## Stack

Python 3.11 | FastAPI | httpx | Pydantic v2 | Ollama (tinyllama) | GKE | Cloud Build

## Archivos clave

```
agent/main.py           → API (endpoints + extracción)
agent/tests/test_main.py → 40 tests
generate_tf.py          → CLI generador de .tf
k8s/                    → Manifiestos K8s
cloudbuild.yaml         → Pipeline de build
```

## Entorno

- **Cluster**: ai-infra-agent (europe-southwest1-a, e2-standard-2 spot, 2 nodos)
- **Namespace**: arturo-llm-test
- **Registry**: europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent
- **NO hay Python local en Windows** — tests se ejecutan en GCloud Shell
- **Sin Cloud NAT** — pods no tienen internet, modelos se cargan manualmente

## Convenciones

- Regiones GCP permitidas: europe-west1/2/3/4, europe-southwest1
- Labels obligatorios: managed-by, project, environment, created-by
- Tests con mocking de Ollama (no requieren cluster ni LLM)
- Builds con `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`

## Notas importantes

- NUNCA borrar el PVC `ollama-pvc` — perderías los modelos LLM cargados
- Los docs/ se mantienen actualizados con errores encontrados y soluciones
- El guion original menciona Jira, pero el proyecto usa Slack en su lugar
