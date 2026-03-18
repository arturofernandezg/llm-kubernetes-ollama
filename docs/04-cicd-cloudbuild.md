# CI/CD — Cloud Build + Artifact Registry

## Pipeline de build

Archivo: `cloudbuild.yaml`

```
gcloud builds submit → Step 1: pytest (64 tests) → Step 2: Docker build → Push a Artifact Registry
                                                                            ├── :latest
                                                                            └── :$COMMIT_SHA
```

El pipeline tiene 2 pasos secuenciales. Si los tests fallan en Step 1,
el build Docker (Step 2) **no se ejecuta** — ninguna imagen rota llega al registry.

## Artifact Registry

| Propiedad | Valor |
|---|---|
| Región | europe-southwest1 |
| Repositorio | aiops-agent |
| Imagen | aiops-agent |
| URL completa | `europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/aiops-agent` |

## Versionado de imágenes

Cada build genera dos tags:
- `:latest` — siempre apunta a la última versión
- `:$COMMIT_SHA` — tag inmutable con el SHA del commit de Git

Esto permite hacer rollback a una versión anterior:
```bash
# Rollback rápido
kubectl set image deployment/agent agent=.../aiops-agent:abc1234 -n arturo-llm-test
```

## Ejecución manual del build

```bash
cd llm-kubernetes-ollama

# $COMMIT_SHA no se rellena automáticamente en builds manuales.
# Hay que pasarlo como sustitución:
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD) \
  .
```

**IMPORTANTE — contexto del build**: el último argumento (`.`) indica que se sube
el directorio raíz del proyecto como contexto. Si se usa `agent/` como contexto,
el paso de Docker no encontrará la carpeta `./agent` porque ya estarás *dentro* de ella.
Error típico: `unable to prepare context: path "./agent" not found`.

**En Windows (Google Cloud SDK Shell)**: no usar `\` para continuación de línea.
Poner todo en una línea: `gcloud builds submit --config cloudbuild.yaml --substitutions=COMMIT_SHA=abc1234 .`

## Dockerfile (`agent/Dockerfile`)

- Base: `python:3.11.12-slim` (versión específica para reproducibilidad)
- Usuario no-root (`appuser`) — buena práctica de seguridad
- Capa de dependencias cacheada (COPY requirements.txt antes que el código)
- Copia todos los módulos Python (`COPY *.py ./`): main.py, config.py, schemas.py,
  extraction.py, validation.py, tf_generator.py (no copia tests ni dev dependencies
  gracias al `.dockerignore`)
- **Nota**: el destino debe terminar en `/` (`COPY *.py ./`, no `COPY *.py .`)
  porque Docker exige directorio explícito cuando hay múltiples ficheros fuente.
  Fix aplicado en commit 326cdc5.

## Deploy tras build

```bash
# Opción A: usar tag específico (recomendado — sabes exactamente qué versión corre)
kubectl set image deployment/agent \
  agent=europe-southwest1-docker.pkg.dev/uniovi-ai-infra-agent/aiops-agent/aiops-agent:326cdc5 \
  -n arturo-llm-test

# Opción B: forzar pull de :latest
kubectl rollout restart deployment/agent -n arturo-llm-test

# Verificar rollout
kubectl rollout status deployment/agent -n arturo-llm-test
```

**Buena práctica**: usar el SHA del commit como tag (`kubectl set image ... :abc1234`)
en vez de `:latest`. Así puedes hacer rollback exacto y sabes qué código está en producción.

---

## Errores conocidos y soluciones

### "invalid image name ... aiops-agent:" (tag vacío)
**Causa**: `$COMMIT_SHA` está vacío porque el build se ejecutó manualmente
sin sustitución. Solo se rellena automáticamente en builds disparados por triggers.
**Solución**: pasar `--substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)`

### Build tarda mucho (>2 min)
**Causa probable**: la capa de `pip install` no está cacheada porque cambió requirements.txt.
**Solución**: no modificar requirements.txt innecesariamente. Cloud Build no cachea
capas Docker entre builds por defecto — considerar `kaniko` para builds con caché.

### "permission to write logs to Cloud Logging"
**Causa**: la service account de Cloud Build no tiene el rol `roles/logging.logWriter`.
**Impacto**: solo afecta a logs, el build funciona igualmente.
**Solución**: `gcloud projects add-iam-policy-binding uniovi-ai-infra-agent --member=serviceAccount:... --role=roles/logging.logWriter`
