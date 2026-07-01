---
name: start
description: "Arranque de sesión del proyecto AIOps. Reconstruye el contexto sin que el usuario tenga que darlo: carga el método de trabajo (AGENTS.md + CLAUDE.md), lee el roadmap (docs/07-roadmap.md) y la última bitácora (docs_sesion/), dice en qué fase estamos y propone el micro-objetivo de hoy. TRIGGERS (EN): 'start', 'start session', 'kick off', 'where were we', 'what's next'. TRIGGERS (ES): 'arranca', 'arrancamos', 'empezamos', '¿dónde estábamos?', '¿qué toca hoy?', 'nueva sesión', 'al lío'. Úsalo al PRINCIPIO de cualquier sesión de trabajo."
---

# /start — Arranque de sesión

Ritual de arranque. Objetivo: que el usuario NO tenga que dar contexto. Sé conciso.

## Paso 1 — Cargar el método de trabajo
Lee `AGENTS.md` (sección "Reglas no negociables" + "Gotchas") y la sección **"Método de trabajo"** de `CLAUDE.md`. Reglas activas no negociables:
- Microtasks (~20 min), flujo **Proposal → Validation → Execution**.
- Comandos shell **siempre en una línea** (el Cloud Shell de empresa rompe multilínea/heredocs).
- **NUNCA** `git add`/`commit`/`push` ni `pytest` — los corre Jay a mano. Provee el comando como texto.
- Commits **sin** `Co-Authored-By`. Conventional Commits.
- Docs/planificación en español, código/comentarios en inglés.
- "Construir despacio y bien"; "docs reflect reality, not ambition".
- Mantener el repo limpio (no binarios, no caché, no duplicados).

## Paso 2 — Reconstruir el estado
1. Lee `docs/07-roadmap.md` → secciones **Estado actual** y **Roadmap a entrega**. Identifica la fase en curso.
2. Lee la entrada más reciente de `docs_sesion/` (por fecha) y cualquiera con frontmatter `promoted: false` → saca el **"Hecho"** y, sobre todo, el **"Siguiente"**.

## Paso 3 — Briefing de arranque (imprímelo, breve)
- **Dónde estamos**: fase actual del roadmap + objetivo de la fase.
- **Última sesión**: 2-3 bullets de lo hecho + lo que quedó en "Siguiente".
- **Gotchas abiertos**: si la última bitácora dejó problemas sin cerrar.

## Paso 4 — Proponer y validar
Propón el **micro-objetivo de hoy** (1, máximo 2) derivado del "Siguiente" + el roadmap.
**Espera confirmación del usuario antes de ejecutar.** No toques ficheros hasta el OK.
