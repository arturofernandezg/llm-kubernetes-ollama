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

### Seguridad del callback human-in-the-loop: HMAC-SHA256 (2026-05-11)

**Por qué es destacable:** El endpoint `/webhook/action` es el gate final de la remediación autónoma — donde el humano aprueba o rechaza. Sin autenticación, cualquier cliente que conociera la URL podría forjar una aprobación.

**Decisión técnica:**

| Capa | Mecanismo | Detalle |
|---|---|---|
| Firma | HMAC-SHA256 por botón | `sign(incident_id:action, WEBHOOK_SECRET)` — la firma cubre **tanto** el incident_id **como** la acción (approve/reject), impidiendo replay cross-action |
| Transporte | Embebido en `context` del botón | Mattermost devuelve el contexto sin modificarlo — no viaja como query param (no aparece en logs de red) |
| Verificación | `hmac.compare_digest` (tiempo constante) | Evita timing attacks en la comparación de strings |
| Backward compat | `WEBHOOK_SECRET` vacío → HMAC desactivado | El pod no falla si el K8s Secret no existe aún (`optional: true`) |
| Configuración | K8s Secret `agent-secrets.webhook-secret` | Nunca en código ni en environment literal |

**Flujo de seguridad actualizado:**
```
send_escalation_with_buttons()
  → sign(incident_id:"approve", secret) → embed en button context
  → sign(incident_id:"reject",  secret) → embed en button context

/webhook/action (callback)
  → _verify_hmac_token(incident_id, action, hmac_token)
     └── si secret vacío → pass-through (dev/test)
     └── si token ausente o erróneo → HTTP 401
  → valida TTL
  → ejecuta / rechaza
```

**Argumento para la defensa:**
> "El endpoint de aprobación humana valida una firma HMAC-SHA256 por botón, binding tanto el UUID del incidente como la acción. Un atacante no puede fabricar un callback válido sin conocer el secreto, y no puede reutilizar la firma de 'rechazar' para aprobar ni viceversa."

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

### Traza E2E completa — Human-in-the-loop (2026-05-06)

Log real del agente en cluster, alert KubePodOOMKilled procesada de punta a punta:

```
confidence=0.90  risk=high  duration=78746ms
action=escalate  commands_total=3  blocked=0
Persisted incident incident-KubePodOOMKilled-1778062371 (outcome=escalate)
Stored pending escalation c907bad7-fe26-49c3-96b2-3b3badeca3a1 (3 commands)
POST http://mattermost-svc.../hooks/...  → HTTP 200 OK
```

**Lo que demuestra cada línea:**

| Línea de log | Qué demuestra |
|---|---|
| `confidence=0.90 risk=high duration=78746ms` | LLM real (qwen2.5:1.5b) inferencia on-cluster, 78.7s |
| `action=escalate commands_total=3 blocked=0` | Motor de decisión aplicó regla 5 (risk=high→escalate); comandos válidos generados |
| `Persisted incident ... (outcome=escalate)` | Feedback loop: la decisión queda en ChromaDB para RAG futuro |
| `Stored pending escalation ... (3 commands)` | Estado en memoria con UUID único; TTL 60 min activo |
| `POST mattermost-svc ... HTTP 200` | Mensaje con botones entregado a Mattermost via cross-namespace DNS |

**Secuencia técnica visible en logs:**
```
Ollama /api/generate (78.7s) →
  ChromaDB POST collections (upsert feedback) →
  Ollama /api/embeddings (embedding del incidente) →
  ChromaDB upsert (persistencia) →
  Mattermost POST webhook (HTTP 200)
```

---

## 5. Framing y narrativa para la defensa

### "Producción real, no una demo"

Tu sistema no es un prototipo local — está desplegado en un cluster GKE real con alertas reales fluyendo. Úsalo como argumento de peso:

- **Cluster real**: GKE europe-southwest1-a, 2 nodos spot e2-standard-2
- **Stack completo desplegado**: Prometheus + Alertmanager + ChromaDB + Ollama + FastAPI + Grafana + Mattermost — todos corriendo en K8s, no en docker-compose local
- **E2E verificado en cluster**: KubePodOOMKilled → RAG → LLM → Mattermost (tiempos reales: 187s, 211s, 78s)
- **Human-in-the-loop E2E**: escalation con botones ✅/❌ en Mattermost — mensaje actualizado in-place al aprobar/rechazar (2026-05-06)
- **Métricas reales**: `aiops_remediation_total{action="escalate"} 3`, `aiops_feedback_total{outcome="persisted"}`, `aiops_remediation_total{action="human_approved"}` (en verificación)

**Tres pilares que defender (inspirados en la presentación de referencia):**

| Pilar | Lo que tienen ellos | Lo que tienes tú |
|---|---|---|
| Seguridad | Whitelisting, OAuth, propagación de roles | NetworkPolicy K8s, RBAC con mínimo privilegio, SecurityContext, reglas de bloqueo en remediation.py, **HMAC-SHA256 en callbacks de botones** |
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

*Última actualización: 2026-05-11*
