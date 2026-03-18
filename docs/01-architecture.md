# Arquitectura del sistema

## Visión general

Agente de IA de ciclo completo para automatizar despliegues de infraestructura GCP
en MasOrange/Telecable. TFG/TFM — rol: ingeniero AIOps.

## Flujo objetivo (zero-touch)

```
Slack message
    │
    ▼
FastAPI Agent ──► Ollama/qwen2.5:1.5b (extracción de parámetros)
    │
    ▼
Terraform Generator ──► genera .tf a partir de módulo corporativo
    │
    ▼
GitHub API ──► crea rama + commit + Pull Request
    │
    ▼
Validation Agent ──► revisa PR vs petición original + políticas internas
    │
    ▼
GitHub Actions ──► terraform plan → aprobación → terraform apply
```

## Flujo actual (Fase 1)

```
[Port-forward / curl / generate_tf.py]
         │
         ▼
   agent-svc:8000      ← FastAPI (1 réplica, probes, retry, metrics, shared httpx client)
         │
         ▼
   ollama-svc:11434    ← Ollama (1 réplica, PVC 20Gi, PDB)
         │
         ▼
     ollama-pvc         ← Modelos persistidos en disco
```

## Componentes

| Componente | Tecnología | Ubicación | Réplicas |
|---|---|---|---|
| Agente | Python 3.11, FastAPI, httpx, Pydantic v2 | `agent/` (6 módulos) | 1 |
| LLM | Ollama (qwen2.5:1.5b) | Pod K8s | 1 |
| TF Generator | Python stdlib (urllib) | `generate_tf.py` | CLI local |
| Build pipeline | Google Cloud Build | `cloudbuild.yaml` | — |
| Infra | GKE (e2-standard-2 spot, 2 nodos) | `k8s/` | — |

## Decisiones de arquitectura

- **Ollama en K8s** (no API externa): control total, sin costes por token,
  datos no salen del cluster. Trade-off: modelos pequeños, menos precisión.
- **PVC ReadWriteOnce**: los modelos sobreviven a reinicios. Trade-off:
  no se puede escalar Ollama a >1 réplica sin migrar a StatefulSet o GCS.
- **Nodos spot**: reduce costes ~60-70%. Trade-off: los pods pueden ser
  desalojados. Mitigado con PDB y PVC.
- **httpx compartido en lifespan**: reutiliza pool de conexiones TCP,
  evita overhead de crear/cerrar cliente por request.
- **Probes separadas**: /healthz (liveness, sin deps) y /readyz (readiness,
  verifica Ollama). Evita crash loops cuando Ollama está down.
- **1 réplica del agente**: originalmente eran 2, pero se redujo a 1
  (commit 8e32b0c) para que todo quepa en un solo nodo spot e2-standard-2
  (~1930m CPU). Con los resources ajustados, es suficiente para Fase 1.
- **qwen2.5:1.5b** en lugar de tinyllama: mejor precisión en extracción de
  parámetros. tinyllama se mantiene como default en config.py para desarrollo local.
- **Código modularizado**: el agente se dividió en 6 módulos (commit 7ec4a3a)
  para facilitar testing unitario y preparar la integración Slack/GitHub de Fase 2.

## Red interna

Todos los servicios usan ClusterIP (no expuestos a internet):
- `agent-svc:8000` → agente FastAPI
- `ollama-svc:11434` → API Ollama
- `apache-svc:80` → servidor web de validación de red

Sin Cloud NAT: los pods no tienen salida a internet. Modelos se cargan
manualmente vía `kubectl exec`.

## Seguridad

- **SecurityContext (agent)**: `runAsNonRoot`, `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`.
  Volume emptyDir montado en /tmp para uvicorn.
- **NetworkPolicy**: 2 políticas que restringen tráfico entre pods:
  - Ollama solo acepta ingress del agent (port 11434)
  - Agent solo acepta ingress de Apache (port 8000)
- **Dockerfile non-root**: usuario `appuser` creado explícitamente.
- **Ollama**: corre como root (limitación de la imagen oficial), pero con
  `allowPrivilegeEscalation: false` y `capabilities.drop: [ALL]`.
