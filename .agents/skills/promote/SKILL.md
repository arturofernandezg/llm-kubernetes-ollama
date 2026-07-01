---
name: promote
description: "Promoción de la bitácora a la documentación real del proyecto AIOps + vault de Obsidian. Lee las entradas de docs_sesion/ con 'promoted: false', destila lo durable a los docs canónicos (07-roadmap, CLAUDE.md, AGENTS.md, 01-06, 11...) y al vault, y marca las entradas como promovidas. Ejecútalo cuando quieras consolidar (fin de sesión grande, cierre de fase). TRIGGERS (EN): 'promote', 'promote docs', 'sync docs', 'consolidate docs'. TRIGGERS (ES): 'promueve', 'promociona la bitácora', 'consolida la documentación', 'pasa la bitácora a docs', 'sincroniza docs', 'ritual de cierre'."
---

# /promote — Bitácora → Documentación canónica + Vault

Proceso de 5 pasos. Exhaustivo pero eficiente. La FUENTE es la bitácora (`docs_sesion/` con `promoted: false`), no solo la conversación — por eso funciona incluso tras un compact.

## Paso 1 — Leer la bitácora pendiente
Lista las entradas de `docs_sesion/` con frontmatter `promoted: false` (o sin frontmatter si son antiguas y relevantes). Léelas. Resume en 5-10 bullets qué contienen colectivamente: qué se hizo, gotchas, decisiones. Etiqueta dominios tocados: `Programming`, `AI_ML`, `Build_Infra`, `Testing`, `Docs`, `Security`.

## Paso 2 — Revisar docs canónicos
Para CADA doc, pregunta: **"¿lo de la bitácora añade, cambia o invalida algo aquí?"** Lee el contenido actual antes de decidir.

1. `docs/07-roadmap.md` — **fuente única de roadmap + estado + changelog**. Actualiza "Estado actual", marca fases, añade al changelog.
2. `CLAUDE.md` — resumen de estado (lean), imagen actual, test count, archivos clave, stack, método de trabajo.
3. `AGENTS.md` — mantén coherente con `CLAUDE.md` si cambió el método/las reglas/los rituales.
4. `docs/02-agent-fastapi.md` — endpoints, schemas, env vars, métricas, logging.
5. `docs/03-kubernetes.md` — manifiestos, probes, NetworkPolicy, RBAC, secrets.
6. `docs/06-testing.md` — tests por fichero, historial, errores conocidos.
7. `docs/08-code-quality-playbook.md` — tabla de sesiones, findings.
8. `docs/11-quality-backlog.md` — backlog vivo (TODO → DONE / WONTFIX).
9. `docs/12-chaos-engineering.md` — experimentos, MTTD/MTTR, hipótesis.
10. `docs/defensa.md` — puntos de presentación/defensa (gitignored; actualizar si hubo trabajo relevante).

**Output**: lista SOLO los docs que necesitan cambios, una línea cada uno. Luego haz todos los cambios.

## Paso 3 — Actualizar el vault de Obsidian
Ruta: `/Users/jay/Library/Mobile Documents/com~apple~CloudDocs/Jay_2nd/Jay/`
- Siempre actualiza el nodo de proyecto `01_Projects/AIOps_Infra_Agent.md` (fecha de estado, test count, patrones, conexiones).
- Por cada dominio del Paso 1, lee solo los ficheros relevantes del área de vault (no globees todo) y actualiza/crea nodos si surgió un patrón reutilizable.
- Reglas de vault: conocimiento extraído DEL proyecto, práctico, inglés, conexiones `[[Node]]`, actualiza la línea `*Created / Updated*`.
- Nodos AIOps a revisar siempre: `03_Knowledge/Programming/Command_Safety_Classification_for_LLM_Actions.md`, `FastAPI_Patterns_for_AI_Services.md`, `ChatOps_Alert_Notification_Pattern.md`.

## Paso 4 — Marcar como promovido
En cada entrada de bitácora procesada, cambia el frontmatter a `promoted: true`. Así `promote` no la reprocesa.

## Paso 5 — Resumen
Imprime: docs actualizados (fichero + cambio en una línea), nodos de vault creados/actualizados, entradas de bitácora marcadas. **NO hagas commit** — lo hace Jay (sin `Co-Authored-By`).
