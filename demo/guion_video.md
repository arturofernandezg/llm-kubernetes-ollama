# Guion video-demo — "Observz" (voz en off + run real en cluster)

**Formato:** screencast híbrido con voz en off sobre un arco chaos REAL capturado en cluster.
**Duración objetivo:** 3:30 – 4:00.
**Audiencia:** chapter (MasOrange/Telecable). Tono prototipo junior honesto, sin humos.
**Principio de edición:** el LLM tarda 147-213s warm en CPU. **Se graba entero y se cortan las esperas muertas en edición** (iMovie). Nunca se acelera el vídeo de forma que engañe: se corta, no se falsea.

---

## Pre-producción (antes de grabar)

**Setup de captura (Mac ARM):**
- Grabación de pantalla: **Cmd+Shift+5** (QuickTime) a pantalla completa, o **OBS** si quieres componer terminal + navegador en escenas separadas.
- Micro: grabar la voz en off **por separado** después (lees este guion sobre el vídeo ya cortado). Más limpio que narrar en directo.
- Resolución: 1080p mínimo. Fuente de terminal grande (que se lea en proyección).

**Ventanas a preparar (una por escena, para poder cortar limpio):**
1. Terminal con `./scripts/chaos_arc.sh` listo para lanzar.
2. Terminal 2 con `kubectl logs -n arturo-llm-test -l app=agent -f` (el razonamiento del agente).
3. Navegador: Mattermost en `#alerts` (canal de escalación).
4. Navegador: Grafana (dashboard Overview + Chaos).
5. `demo/demo_v3.html` abierto (slide 1 para el cold open, slides de cierre para evidencia).

**Pre-vuelo (lo hace el propio `chaos_arc.sh`, pero confírmalo):**
- Cluster arriba, `aiops-agent:2ac3c5d` desplegado, `/readyz` = 200.
- Redis sin residuos (el script lo limpia, pero verifica que el smoke test no dejó cooldown+índice).
- Ollama warm (port-forward + una query previa para que el primer diagnóstico no cargue el modelo en frío).
- Port-forwards: agente `:8000`, Grafana, Mattermost accesibles.

---

## Guion por escenas

> Notación: **[PANTALLA]** = qué se ve · **🎙️** = voz en off · **✂️** = punto de corte en edición.

### Escena 1 — Cold open (0:00 – 0:20)

**[PANTALLA]** `demo_v3.html` slide 1 ("Observz" + la tesis).

🎙️ *"Esto es un agente de remediación automática para Kubernetes. Detecta un fallo, lo diagnostica con un modelo de lenguaje, lo consulta con un humano, aplica el arreglo y verifica si funcionó. Una frase lo resume: el clúster informa, el modelo razona, el motor dispone. Vamos a verlo con un fallo real, en un clúster real."*

✂️ Corte a terminal.

---

### Escena 2 — El fallo (0:20 – 0:50)

**[PANTALLA]** Terminal 1: lanzas `./scripts/chaos_arc.sh`. Se ve el pre-vuelo (`/readyz: 200`, "Redis sin residuos") y el `kubectl apply` del OOM. En paralelo, Grafana o `kubectl get pods -n arturo-chaos -w` mostrando el pod entrando en `OOMKilled` / `CrashLoopBackOff`.

🎙️ *"Inyectamos un fallo controlado: un pod que consume más memoria de la que tiene asignada. Kubernetes lo mata por OOM una y otra vez. Prometheus lo detecta, dispara la alerta, y Alertmanager la manda por webhook a nuestro agente."*

✂️ Corte. **Aquí empieza la espera del LLM — se corta en edición.**

---

### Escena 3 — El razonamiento con grounding (0:50 – 1:35)

**[PANTALLA]** Terminal 2: logs del agente. Resalta (zoom o highlight en edición):
- El snapshot de enrichment: `current_value=32Mi` (del clúster, no del modelo).
- Los `CLUSTER FACTS` en el prompt (limits, restart_count, last_state_reason, logs/events).
- El diagnóstico JSON del modelo + `grounded=1.0`.

🎙️ *"Aquí está la diferencia con un chatbot. Antes de preguntar al modelo, el agente fotografía el estado real del clúster: cuánta memoria tiene el contenedor —treinta y dos megas—, cuántas veces ha reiniciado, qué dicen sus logs y eventos. Esos hechos entran en el prompt. El modelo no adivina el estado: lo recibe. Por eso la propuesta va etiquetada como 'grounded' — anclada a datos reales, no a una alucinación."*

✂️ Corte a Mattermost.

---

### Escena 4 — Human-in-the-loop (1:35 – 2:20)

**[PANTALLA]** Navegador: Mattermost `#alerts`. Se ve la escalación con el **doble botón**:
- `approve_engine` → determinista, ×2 (64Mi).
- `approve_model` → valor del modelo (512Mi).

Haces click en **Aprobar el valor del modelo (512Mi)** — es el que CURA con este manifiesto; el del motor (×2=64Mi) acabaría en `rolled_back` (validado en S7). Se ve la confirmación.

🎙️ *"El agente no actúa solo. Escala a Mattermost con dos opciones: una determinista, calculada por el motor —el doble de la memoria actual—; y otra con el valor que propone el modelo. Cada botón firma su propia acción con HMAC, así que la elección va protegida criptográficamente: nadie puede falsificar qué se aprobó. El operador decide. Aprobamos."*

✂️ Corte. Ventana de rollback (300s) — se corta en edición.

---

### Escena 5 — El veredicto (2:20 – 3:00)

**[PANTALLA]** Terminal 2: el log del arco. Resalta:
- El patch aplicado (`kubectl set resources` determinista).
- El resultado del arco: `outcome=cured` (o `rolled_back` si mostraste el botón ×2 de 64Mi).
- El re-upsert R2 a ChromaDB con el veredicto.

🎙️ *"El agente aplica el arreglo y NO se olvida. Abre una ventana de observación: si el pod sigue muriendo, hace rollback automático. Aquí está lo interesante: el botón del motor, el doble de memoria, no basta contra este fallo —solo el valor del modelo, quinientos doce megas, lo cura. El sistema lo verifica solo y escribe el veredicto: 'curado' o 'revertido'. Y ese veredicto vuelve a la base de conocimiento: el sistema aprende de su propia decisión."*

✂️ Corte a Grafana.

---

### Escena 6 — La evidencia (3:00 – 3:40)

**[PANTALLA]** Navegador: Grafana. Panel `aiops_incident_resolution_seconds{error_class="OOMKilled"}` (~92s), la fila de la Cola (`aiops_queue_*`), el dashboard de Chaos.

🎙️ *"Nada de esto es una caja negra. Cada paso está instrumentado: cuánto tardó en resolverse el incidente —noventa y dos segundos—, cuántas alertas hay en cola, el estado de cada componente. Observabilidad desde el día uno, no como añadido final. El clúster informa, el modelo razona, el motor dispone, y todo queda medido."*

✂️ Corte a slide de cierre.

---

### Escena 7 — Cierre (3:40 – 4:00)

**[PANTALLA]** `demo_v3.html` slide de cierre (arquitectura o tesis).

🎙️ *"Un agente que detecta, razona con datos reales, consulta al humano, remedia y verifica. Corriendo en un clúster de Kubernetes, con un modelo pequeño en CPU. Gracias."*

FIN.

---

## Post-producción (checklist)

- [ ] Cortar las 3 esperas del LLM/rollback (escenas 2→3, 4→5). El vídeo bruto son ~12-15 min; el final son ~4.
- [ ] Añadir zooms/highlights en los datos clave (`current_value=32Mi`, `grounded=1.0`, doble botón, `outcome=`, `92.47s`).
- [ ] Grabar la voz en off leyendo este guion sobre el vídeo ya cortado.
- [ ] Rótulo inicial con título + fecha (opcional).
- [ ] Export 1080p, formato compatible (H.264 .mp4).
- [ ] **Plan B:** deja el arco corriendo con `--keep` una vez para tener capturas fijas de respaldo por si un corte sale mal.

## Notas de honestidad (principio "docs reflect reality")

- Se **cortan** las esperas, no se aceleran de forma engañosa. Si alguien pregunta "¿cuánto tardó de verdad?", la respuesta honesta es: ~3-4 min de LLM en CPU, comprimidos en edición.
- El run es real: OOM real, snapshot real, patch real, veredicto real. No hay mockups.
- El `rolled_back` no es un fallo que ocultar: es el safety-net funcionando. Si lo muestras, es munición a favor, no en contra.
