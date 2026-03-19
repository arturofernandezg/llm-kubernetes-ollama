# Roadmap — Fases del proyecto (Evolución AIOps)

## Estado de las fases

| Fase | Descripción | Estado |
|---|---|---|
| Fase 1 (Legado) | Agente + Ollama + extracción de params + generación .tf | Completa (En desuso activo) |
| Fase 1 (Observabilidad) | Enrutador de alertas de Prometheus, webhook FastAPI y Mattermost | Pendiente |
| Fase 2 (RAG & Vertex AI)| Ingesta de contexto en BBDD Vectorial In-cluster (ChromaDB) | Pendiente |
| Fase 3 (Remediación)  | Ejecución automática segura en la Kubernetes API Server | Pendiente |

---

## Fase 1 (Legado) — Completada

- [x] Agente FastAPI con extracción de parámetros via LLM
- [x] Ollama desplegado en K8s con PVC
- [x] Generador de Terraform (generate_tf.py + agent/tf_generator.py)
- [x] 64 tests unitarios y de integración (4 ficheros)
- [x] Cloud Build con versionado ($COMMIT_SHA + :latest) y tests como gate
- [x] Cliente httpx compartido (no uno nuevo por request)
- [x] PodDisruptionBudget para Ollama
- [x] Código modularizado: config.py, schemas.py, extraction.py, validation.py, tf_generator.py

---

## Fase 1 — Observabilidad y ChatOps (ACTUAL)

### Enrutamiento de Metadatos
- [ ] Instalación del stack `kube-prometheus-stack` (Prometheus + Alertmanager).
- [ ] Implementar un nuevo endpoint `POST /webhook/alert` en FastAPI para ingerir y normalizar alertas.
- [ ] Conectar la capa de eventos de Alertmanager con el Webhook de FastAPI.

### ChatOps (Mattermost)
- [ ] Instalar helm-chart de Mattermost en un namespace aislado.
- [ ] Generar e integrar token bot/webhook de Mattermost.
- [ ] Desarrollar lógica transaccional: el Agente FastAPI envía diagnósticos generados por el LLM directo al hilo de la alerta en Mattermost.

---

## Fase 2 — Base de Conocimiento Vectorial (RAG)

- [ ] Migrar el Node Pool principal de instancias `Spot` a `Standard` para asegurar la persistencia y evitar corrupción en las BBDD en memoria.
- [ ] Desplegar **ChromaDB** o motor vectorial equivalente dentro del clúster (In-Cluster deployment).
- [ ] Implementar framework RAG dentro del Agente FastAPI.
- [ ] Alimentar la base de conocimiento vectorial con runbooks históricos, logs y casos previos de MasOrange/Telecable.
- [ ] **AIOps Inference Engine**: Adaptar la consulta del LLM para que, ante una alerta, la envuelva con el contexto más similar extraído de la BBDD vectorial.
- [ ] Definir topología de Red para consumir la inferencia pesada (Claude 3.5 Sonnet / Gemini 1.5 Pro) vía Vertex AI habilitando *Private Google Access*, sin salir a Internet Público.
- [ ] Experimentar validaciones con Jupyter Notebooks / Vertex LLM Notebooks.

---

## Fase 3 — Autonomía de Remediación

- [ ] Ampliación Least-Privilege de RBAC (Role-Based Access Control) del Agente FastAPI para poder hacer `.patch()` y `.update()` de recursos K8s (`Deployments`, `LimitRanges`, `Pods`).
- [ ] Desarrollar pipelines de validación automatizada: 
  - Si el modelo IA estima un aumento de recursos de memoria `< 25%` -> **Aprobar automáticamente**.
  - Si exige reconfigurar arquitectura o aumentos de capacity excedentes -> **Solicitar confirmación de humano en Mattermost**.
- [ ] Monitorización de Bucle Cerrado: comprobar que las alertas cesan en Prometheus tras aplicar medidas técnicas sugeridas por el agente.

---

## Cronograma de Ejecución (Roadmap 2 Meses - 8 Semanas)

Dado un horizonte temporal de 2 meses aplicable al ciclo final del proyecto (TFM/TFG), el ritmo de ejecución semanal se articula de la siguiente manera:

### MES 1: Observabilidad, Ingesta y ChatOps (Fase 1)
- **Semana 1-2 (Observabilidad Base y Chat)**: Despliegue de `kube-prometheus-stack` (Prometheus + Alertmanager) y de la plataforma Mattermost en Kubernetes. Definición y sintaxis de las alarmas críticas (ej. PodOOMKilled, CPU Throttling).
- **Semana 3-4 (Routing Webhook)**: Validación del *Private Google Access* a nivel de subred VPC GCP con el tutor. Desarrollo del Webhook en FastAPI (`/webhook/alert`) capaz de capturar el payload JSON de Alertmanager, limpiarlo y renderizarlo en un mensaje parseado legible hacia un canal privado en Mattermost.

### MES 2: Infraestructura RAG y Bucle de Remediación (Fases 2 y 3)
- **Semana 5-6 (Capa RAG In-Cluster)**: Migración en GKE de Nodos K8s Spot a Standard. Despliegue persistente de la BBBDD ChromaDB In-cluster. Desarrollo del código puente para Vertex AI (Gemini/Claude). El webhook ahora, antes de avisar en Mattermost, consulta el contexto a ChromaDB y adjunta la recomendación AIOps "Zero-shot".
- **Semana 7-8 (Autonomía y Cierre)**: Ajustes restrictivos de RBAC de K8s para autorizar al ServiceAccount de FastAPI a hacer `.patch()` sobre LimitRanges/Deployments. Inclusión del motor de confirmación interactiva (botones de "Rechazar" / "Aprobar Remediación" en Mattermost). Pruebas globales simulando la desconexión total por OOM y redacción final de la memoria descriptiva del proyecto.

---

## Mejoras técnicas pendientes (transversales)

- [ ] Evaluar framework retrospectivemente (ej. Robusta.dev) para futuras iteraciones de AIOps en la captura contextual de K8s.
- [ ] Mapeo de ServiceAccounts de Kubernetes (Workload Identity) a IAM GCP para comunicación bidireccional segura.
- [ ] Solicitar rol `roles/logging.logWriter` para service account de Cloud Build (warning actual no afecta builds, pero ensucia logs).
