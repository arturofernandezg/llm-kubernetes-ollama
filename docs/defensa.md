# Defensa TFG/TFM — Puntos Clave

Documento vivo. Se actualiza automáticamente durante las sesiones de trabajo cuando aparecen temas relevantes para la defensa.

---

## 1. Datos sucios y calidad del dato

**Por qué lo van a preguntar:** Es un problema clásico en sistemas AIOps. Si el tribunal vio la slide sobre "Datos sucios y silos de conocimiento", van a querer saber cómo lo atacas.

**Tu respuesta — capas de defensa en el sistema:**

| Punto de entrada | Problema potencial | Mecanismo en el código |
|---|---|---|
| Alertas Prometheus → webhook | Campos vacíos, labels inconsistentes, duplicados | Validación Pydantic v2 en `schemas.py`; `startsAt` obligatorio en `AlertItem` |
| Runbooks → ChromaDB (RAG) | Documentación obsoleta o mal redactada | Curación manual de 16 runbooks como decisión de diseño consciente |
| Output LLM → extracción | JSON malformado, campos ausentes | `extraction.py` con 3 estrategias de extracción + fallback encadenado |
| Valores de remediación | Unidades de memoria inconsistentes, valores absurdos | `parse_memory_to_bytes()` normaliza; regla 4.6 bloquea si `new > 2× current` |

**Argumento para la defensa:**
> "El sistema no confía ciegamente en el LLM. Cada dato que entra a la capa de remediación pasa por validación de esquema, extracción robusta con fallback, y reglas de negocio deterministas que actúan como safety net frente a datos sucios o alucinaciones."

---

## 2. Decisiones de diseño destacables

### Human-in-the-loop: escalado con botones interactivos (2026-05-06)

**Por qué es destacable:** El sistema no es binario (auto o nada). Para casos de riesgo alto o comandos que implican reinicio de pod, el agente escala al operador con un mensaje de Mattermost que incluye botones de acción. El humano aprueba o rechaza sin salir del chat.

**Diseño técnico:**

| Decisión | Alternativa descartada | Razón |
|---|---|---|
| Estado en memoria (dict + TTL 60 min) | Redis / ChromaDB como backend de estado | Sin dependencias nuevas; suficiente para TTL de demo |
| Callback inline (respuesta JSON actualiza el mensaje) | Bot token de Mattermost | El incoming webhook ya soporta `update` en la respuesta — sin secrets adicionales |
| Solo `safe_commands` aprobables | Todos los comandos | Los comandos en blacklist (`kubectl delete`, `drain`...) nunca son ejecutables aunque el humano apruebe |

**Flujo de seguridad:**
```
ESCALATE + safe_commands → Mattermost [✅ Aprobar] [❌ Rechazar]
   ↓ click
/webhook/action → valida TTL → ejecuta (si DRY_RUN=false) → actualiza mensaje → persiste en ChromaDB
```

**Argumento para la defensa:**
> "El sistema no toma decisiones autónomas en casos de riesgo. El motor de decisión actúa como primer filtro determinista; el humano actúa como segundo filtro para comandos que implican cambios estructurales. La decisión final queda auditada en ChromaDB como feedback para el RAG."

---

### Vendor Lock-in cero en la capa de IA

**Ángulo:** El sistema es agnóstico de proveedor en todos sus niveles.

| Capa | Decisión | Impacto |
|---|---|---|
| Modelo LLM | Ollama como abstracción — cambias `OLLAMA_MODEL` en config y usas otro modelo sin tocar código | Swap de qwen2.5 a Llama3 o Mistral sin reescribir nada |
| Vector DB | ChromaDB self-hosted on-cluster | Sin APIs de pago externas (OpenAI Embeddings, Pinecone...) |
| Alerting | Webhook sigue el estándar Alertmanager | Compatible con cualquier stack Prometheus, no solo GKE |
| Infraestructura | Manifiestos K8s estándar | Portable a EKS, AKS o cualquier cluster K8s |

**Argumento para la defensa:**
> "El stack de IA es 100% self-hosted y agnóstico de proveedor. Cambiar de modelo, de cluster o de proveedor cloud es una operación de configuración, no de ingeniería."

---

## 3. Preguntas difíciles anticipadas

*(Se irá completando)*

---

## 4. Métricas y evidencias para mostrar

*(Se irá completando)*

---

## 5. Framing y narrativa para la defensa

### "Producción real, no una demo"

Tu sistema no es un prototipo local — está desplegado en un cluster GKE real con alertas reales fluyendo. Úsalo como argumento de peso:

- **Cluster real**: GKE europe-southwest1-a, 2 nodos spot e2-standard-2
- **Stack completo desplegado**: Prometheus + Alertmanager + ChromaDB + Ollama + FastAPI + Grafana + Mattermost — todos corriendo en K8s, no en docker-compose local
- **E2E verificado en cluster**: KubePodOOMKilled → RAG → LLM → Mattermost (documentado con tiempos reales: 187s, 211s)
- **Métricas reales**: `aiops_remediation_total{action="escalate"} 2`, `aiops_feedback_total{outcome="persisted"} 2`

**Tres pilares que defender (inspirados en la presentación de referencia):**

| Pilar | Lo que tienen ellos | Lo que tienes tú |
|---|---|---|
| Seguridad | Whitelisting, OAuth, propagación de roles | NetworkPolicy K8s, RBAC con mínimo privilegio, SecurityContext, reglas de bloqueo en remediation.py |
| Escalado | Pods escalan por volumen de peticiones | Spot instances GKE, retry/backoff en mattermost.py, HTTP_TIMEOUT=240 para LLM lento |
| Observabilidad | Logs + métricas + agent tracing | JSON logging estructurado, 6 counters aiops_* en Prometheus, Grafana dashboard 9 paneles, feedback persistido en ChromaDB |

---

---

## 6. Presentación — referencia y stack técnico

**Referencia:** Presentación de otro proyecto AIOps vista el 2026-05-05 (localhost:5173).

**Stack que usaron:** Vite + React, desplegado en local.

**Workflow de creación:** Perplexity (investigación) → Gemini (scaffolding inicial) → Claude Code (refinado).

**Ideas de estructura visual a adaptar:**
- Slide de "El desafío" con 3 cards (problema humano + datos + info enterrada)
- Slide de "Producción real, no una demo" con logos del stack tecnológico
- Slide "Protocolos y Flexibilidad — Evitando el Vendor Lock-in" con tabla de componentes
- Tres pilares de arquitectura: Seguridad / Escalado / Observabilidad

**Decisión pendiente:** ¿Vite + React o alternativa más simple (Slidev, Reveal.js)?

---

*Última actualización: 2026-05-05*
