# Demo 1 — Reunión tutor (2026-04-20)

Guion para la reunión de las ~13:00. Diseñado para ~10 min de demo + 5 min de Q&A.

---

## Resumen de 30 segundos (con esto abrir la reunión)

> "Tengo el sistema AIOps completo desplegado en GKE: detecta alertas Kubernetes, hace RAG sobre 16 runbooks propios, genera diagnóstico con LLM local y persiste el incidente. Esta mañana he cerrado el último bloqueante (notificación a Mattermost) y voy a hacer una demo end-to-end en vivo. Después tengo dos preguntas pendientes."

---

## Qué mostrar, en orden

### 1. Arquitectura — pizarra o diagrama (1 min)

Dibujar/mostrar:

```
Alertmanager → FastAPI (/webhook/alert) → RAG (ChromaDB + nomic-embed-text)
                                              ↓
                        Diagnosis (qwen2.5:1.5b en Ollama)
                                              ↓
                Remediation gate (suggest_only) ← seguridad
                                              ↓
                Persist incident (ChromaDB) + Mattermost notify
```

Puntos a destacar:
- **Todo on-cluster**: no hay llamadas a APIs externas (LLM local, embeddings locales, RAG local).
- **Sin Cloud NAT**: el cluster no tiene salida a internet, los modelos se cargaron manualmente.
- **Capa de seguridad explícita**: el agente nunca ejecuta nada por su cuenta sin gate.

### 2. Demo en vivo — alerta firing (3 min)

Tener dos terminales abiertas. En la 1 (logs en streaming):

```bash
kubectl logs -n arturo-llm-test deploy/agent -f --tail=20
```

En la 2 (disparar la alerta):

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python /tmp/test_webhook.py
```

Lo que el tutor verá en el log de la terminal 1:

```
Alert webhook received, alerts_count=1
Processing alert 1/1, alertname=KubePodOOMKilled
RAG retrieval: 3 runbooks, 0 incidents
Diagnosis generated: confidence=0.85 risk=high duration=~36s
Remediation decision: action=suggest_only, risk=high, commands_total=3
Persisted incident incident-KubePodOOMKilled-...
Mattermost notification sent
```

Después, abrir Mattermost en el navegador y mostrar el mensaje renderizado en el canal (markdown con bold + bloques de código + emojis).

**Esta es la prueba visual de que las 5 capas se encadenan correctamente.**

### 3. Demo en vivo — alerta resolved (1 min)

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python /tmp/test_resolved.py
```

Mostrar el mensaje `🟢 [RESOLVED] [CRITICAL] KubePodOOMKilled` que aparece en el canal. Sin LLM, ruta directa por código (más rápida).

### 4. Explicar el gate de seguridad (1 min)

En el log de la demo aparece `action=suggest_only`. Explicar:
- El módulo `remediation.py` clasifica los comandos sugeridos por el LLM (read/write/destructive).
- Por configuración actual, **el agente nunca ejecuta**, solo sugiere.
- Cuando se quiera activar: flag explícito + RBAC namespace-scoped (que probamos a continuación).
- Esto es defendible como diseño (human-in-the-loop), no como limitación.

### 5. Feedback loop (30 s)

Cada incidente se persiste en una colección `incidents` de ChromaDB. Esto cierra el ciclo: futuros retrievals incluirán incidentes pasados como contexto adicional para el LLM.

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python -c "from agent.rag import RagClient; r = RagClient(); print('runbooks:', r.count('runbooks'), 'incidents:', r.count('incidents'))"
```

(Comando exacto puede variar según API de `rag.py` — verificar antes de la demo.)

---

## Estado cuantitativo (slide o resumen verbal)

| Métrica | Valor |
|---|---|
| Tests unitarios pasando en CI | 193 |
| Runbooks indexados | 16 |
| Pipeline end-to-end | ✅ Operativo |
| Notificación Mattermost | ✅ Funcionando (URL persistida en Secret) |
| RBAC | ⏳ Pendiente de aplicar (siguiente paso, en vivo si hay tiempo) |

---

## Preguntas para el tutor

### Pregunta 1 — Alerting rules sin Prometheus operator

**Contexto**: el cluster no tiene Prometheus desplegado y kube-prometheus-stack requiere ClusterRoles que no tengo. Alertmanager standalone está corriendo, pero **Alertmanager no evalúa rules** (eso es trabajo de Prometheus).

**3 opciones, en orden de preferencia**:

- **B (recomendable)**: desplegar un Prometheus standalone mínimo (solo Deployment + ConfigMap con `rule_files`) en `arturo-monitoring`. No requiere ClusterRoles si scrapea solo ese namespace y los pods del agente.
- **A**: pedir los ClusterRoles necesarios para kube-prometheus-stack (operator completo). Más limpio pero requiere más permisos.
- **C**: seguir simulando alertas vía `curl POST /api/v2/alerts` al Alertmanager. Funciona para demo y desarrollo pero no es production-ready.

→ **Pregunta concreta**: ¿prefieres que vaya por B (Prometheus standalone mínimo) o pides los ClusterRoles para A?

### Pregunta 2 — Confirmar permiso `container.roles.create`

Me confirmaste que me lo concediste. **Si tengo tiempo en la demo, lo pruebo en vivo**:

```bash
kubectl apply -f k8s/rbac.yaml
kubectl get role,rolebinding -n arturo-llm-test
```

Si aplica OK, la Fase 3 (remediación real) deja de estar bloqueada por permisos. Yo lo dejaría en `suggest_only` por defecto y solo lo flippearía bajo aprobación, pero la infraestructura RBAC ya estaría lista.

---

## Lo que falta por hacer ANTES de la reunión (~50 min)

Estos pasos los ejecuto justo antes de la reunión para tener la demo bulletproof:

### Paso A — Confirmar Mattermost con Secret (2 min)

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python -c "import httpx, os; r = httpx.post(os.environ['MATTERMOST_WEBHOOK_URL'], json={'text':'test post-secret'}, timeout=10.0); print(r.status_code, r.text)"
```

Esperado: `200 ok`. Si falla → debugging antes de continuar.

### Paso B — Ensayar e2e firing (5 min)

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python /tmp/test_webhook.py
kubectl logs -n arturo-llm-test deploy/agent --tail=30
```

Verificar en Mattermost que llega el mensaje. **Captura de pantalla** del canal + del log.

### Paso C — Crear y ensayar e2e resolved (10 min)

Crear el script `/tmp/test_resolved.py` en el pod del agente. Comando one-liner para Cloud Shell:

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python -c "open('/tmp/test_resolved.py','w').write('import httpx; payload={\"version\":\"4\",\"status\":\"resolved\",\"alerts\":[{\"status\":\"resolved\",\"labels\":{\"alertname\":\"KubePodOOMKilled\",\"severity\":\"critical\",\"namespace\":\"arturo-llm-test\",\"pod\":\"nginx-test-abc123\",\"container\":\"nginx\"},\"annotations\":{\"summary\":\"Pod OOMKilled (resolved)\",\"description\":\"Pod stable for 5m\"},\"startsAt\":\"2026-04-20T10:00:00Z\",\"endsAt\":\"2026-04-20T10:05:00Z\"}]}; r=httpx.post(\"http://localhost:8000/webhook/alert\", json=payload, timeout=30.0); print(r.status_code, r.text)')"
```

Luego ejecutar:

```bash
kubectl exec -n arturo-llm-test deploy/agent -- python /tmp/test_resolved.py
```

Verificar mensaje verde `🟢 [RESOLVED]` en Mattermost. **Captura de pantalla**.

### Paso D — Verificar que el e2e firing aún tiene el script (sanity check)

```bash
kubectl exec -n arturo-llm-test deploy/agent -- ls -la /tmp/
```

Si `/tmp/test_webhook.py` no está (puede haberse perdido en el rolling update porque `/tmp` es `emptyDir`), hay que recrearlo. Mismo patrón que el resolved pero con `status: "firing"` y los datos del Bloque 4.2 del plan original (`docs_sesion/2026-04-17.md`).

### Paso E (opcional, si sobra tiempo) — Aplicar RBAC

```bash
kubectl apply -f k8s/rbac.yaml
kubectl get role,rolebinding -n arturo-llm-test
```

Si OK → ya tengo material para mostrarle al tutor que la Fase 3 está desbloqueada. Si falla → lo aplico en vivo durante la demo (es interesante mostrar que aún hay un permiso por refinar).

---

## Plan B si algo falla en vivo

1. **Mattermost no responde**: ya tengo capturas previas del mensaje funcionando — mostrar capturas, explicar el debugging que hice (URL apuntaba al port-forward de Cloud Shell que ya no existía, lo solucioné moviendo a Secret con la URL del service interno).
2. **LLM tarda demasiado en demo**: explicar que son 36s en spot nodes e2-standard-2; en producción se usaría un modelo más grande con GPU dedicada o un endpoint gestionado. La latencia actual es aceptable para alertas K8s (no son tiempo real).
3. **RBAC falla**: ya tengo el output del error documentado, lo enseño y discutimos qué permiso adicional puede faltar.

---

## Después de la reunión

Documentar las decisiones del tutor en `docs_sesion/2026-04-20.md`:
- Qué opción eligió para alerting rules (A/B/C).
- Si aprueba activar Fase 3 (ejecución real de remediaciones) o mantenerlo en suggest_only.
- Cualquier feedback sobre la arquitectura o el approach.

Y actualizar `docs/07-roadmap.md` con la decisión.
