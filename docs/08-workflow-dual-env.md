# Workflow Dual-Environment (Mac personal + PC empresa)

## Por qué este flujo

El PC de empresa tiene acceso a GCP/GKE pero no es cómodo para programar.
El Mac personal es donde se desarrolla, pero no tiene credenciales ni acceso al cluster.

Solución: desarrollar aquí, desplegar allá, traer resultados de vuelta.

---

## Ciclo de trabajo

```
Mac personal (Claude Code)          PC empresa (Cloud Shell)
─────────────────────────           ────────────────────────
1. Escribir código + tests
2. Correr tests locales
3. Preparar DEPLOY_CHECKLIST.md
4. git push
                          ───────►
                                    5. git pull
                                    6. Ejecutar checklist paso a paso
                                    7. Guardar outputs (deploy-log.txt)
                                    8. git push (o pegar en chat)
                          ◄───────
9. Revisar resultados
10. Si falla → fix → nuevo push
11. Si OK → siguiente tarea
```

---

## Qué se hace en cada máquina

### Mac personal (este repo)

- Todo el código: módulos nuevos, refactors, fixes
- Todos los tests (mockeados, sin infra real)
- Manifiestos K8s (YAML) — se editan aquí, se aplican allá
- Documentación
- Preparar checklists de deploy

### PC empresa (Cloud Shell)

- `kubectl apply` de manifiestos
- Configuración de servicios (Mattermost webhooks, etc.)
- Carga manual de modelos en Ollama
- Tests end-to-end reales (curl al cluster)
- Troubleshooting de pods (logs, describe, etc.)

---

## Cómo pasar resultados del PC empresa a Claude Code

### Opción 1: Fichero de log en el repo (recomendada)

Volcar la salida de cada comando a un fichero con `tee`:

```bash
# Ejemplo: aplicar manifest y guardar output
kubectl apply -f k8s/chromadb.yaml 2>&1 | tee deploy-log.txt

# Añadir más outputs al mismo fichero
kubectl get pods -n arturo-llm-test 2>&1 | tee -a deploy-log.txt

# Curl de prueba
curl -s -X POST http://agent-svc:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d @test-payload.json 2>&1 | tee -a deploy-log.txt
```

Luego `git add deploy-log.txt && git commit && git push`. En el Mac, `git pull` y Claude Code lo lee.

> **Nota**: borrar `deploy-log.txt` del repo una vez revisado para no acumular basura.

### Opción 2: Pegar en el chat

Para outputs cortos (un `kubectl get pods`, un error puntual), pegar directamente en la conversación de Claude Code.

### Opción 3: Screenshots

Para cosas visuales (UI de Mattermost, dashboards), guardar la imagen en cualquier ruta local y decirle a Claude Code la ruta — puede leer imágenes.

---

## DEPLOY_CHECKLIST.md

Cada vez que se complete una tarea de desarrollo, se genera un fichero `DEPLOY_CHECKLIST.md` en la raíz del proyecto con:

1. **Qué ejecutar** — comandos exactos para copy-paste
2. **Qué debería pasar** — output esperado para cada paso
3. **Qué guardar** — qué outputs traer de vuelta
4. **Qué hacer si falla** — troubleshooting básico

Este fichero se reescribe con cada nueva tanda de trabajo. Es desechable, no histórico.

---

## Activar el entorno en el Mac

```bash
cd agent
source .venv/bin/activate
python -m pytest tests/ -v          # verificar que todo pasa
```

---

*Creado: 2026-03-30*
