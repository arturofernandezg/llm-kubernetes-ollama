# AGENTS.md — Contexto para agentes (Codex y compatibles)

> Este fichero es el equivalente a `CLAUDE.md` para agentes que leen `AGENTS.md`
> (Codex CLI/IDE y compatibles). **Fija el MÉTODO de trabajo; el detalle del proyecto
> vive en `CLAUDE.md` y `docs/`.** Léelos: no dupliques estado aquí (evita drift).

## Fuente de verdad

1. **`CLAUDE.md`** (raíz) — contexto completo del proyecto: resumen, stack, archivos clave, entorno, convenciones, notas. **Léelo primero.**
2. **`docs/07-roadmap.md`** — fuente única de estado + roadmap + changelog + modos de fallo + backlog.
3. **`docs/`** — un fichero por parte del sistema (ver tabla en `CLAUDE.md`). **Lee el doc relevante antes de tocar esa parte.**
4. **`docs_sesion/`** — bitácora cruda por sesión (capa append-only). La última entrada dice dónde estábamos.

## Quién es Jay (usuario)

- Ingeniero de telecomunicaciones, especialista DSP. macOS ARM (M4) con **acceso directo a GCP/GKE**.
- Español nativo (docs y planificación en español); inglés para código, comentarios y vault.
- Sesiones microtask (~20 min). Prefiere **enviar código que funciona** a discutir diseño.
- Espera nivel **Senior Software Engineer**. **Cero tolerancia a alucinaciones**: si no estás seguro, dilo y levanta la mano.

## Reglas no negociables (así trabajamos)

1. **Flujo Proposal → Validation → Execution.** Propón el micro-objetivo, espera OK, luego ejecuta. Recomienda con postura; no enumeres opciones sin más.
2. **Microtasks (~20 min).** Un objetivo por sesión (máximo dos).
3. **NUNCA** `git add` / `commit` / `push` ni `pytest`. Los corre Jay a mano. Provee el comando **como texto**.
4. **Commits sin `Co-Authored-By`.** Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`).
5. **Comandos shell SIEMPRE en una línea.** El Cloud Shell de empresa rompe multilínea/heredocs. Sin `\` de continuación, sin heredocs.
6. **Docs en español, código y comentarios en inglés.**
7. **Docs son BLOCKING**: no hay commit sin docs actualizados. "Docs reflect reality, not ambition" — describe lo que EXISTE en el código; lo aspiracional va al roadmap como bullets concisos.
8. **"Construir despacio y bien."** Fundamentos sólidos > prototipos de usar y tirar. Test-driven: lógica y tests en paralelo.
9. **Security by design / Observability first.** Nunca commitees secretos (usa `.env.example`). Logging e instrumentación desde el día 1.
10. **Repo limpio**: sin binarios, sin caché, sin duplicados.

## Gotchas del entorno (aprendidos, no obvios)

- **Cluster GKE compartido** con una compañera → SIEMPRE `-n <namespace>` explícito; nunca toques namespaces ajenos. Namespaces propios: `arturo-*`.
- **Sin Cloud NAT**: los pods no tienen internet; los modelos LLM se cargan a mano. NUNCA borres el PVC `ollama-pvc`.
- El **tag de deploy es SIEMPRE el short SHA (7 chars)** del commit, nunca el build ID de Cloud Build (da `ImagePullBackOff`). Imagen: `aiops-agent:<shortsha>`.
- Para **mirror de imágenes** usa `crane copy --platform linux/amd64` (docker pull+tag+push falla en silencio en Mac ARM).
- Al documentar despliegues/verificaciones, incluye **runbooks completos** (todos los port-forwards + flujo E2E de todos los servicios).
- Tests con **mocking de Ollama** (no requieren cluster ni LLM). `startsAt` es obligatorio en payloads `AlertItem` de test.
- Nunca uses recursos **cluster-scoped de escritura** (ClusterRole/Binding de escritura) — convención del proyecto.

## Los rituales (skills)

Tres skills en `.agents/skills/` reproducen nuestro ciclo de sesión. Codex las descubre por
`name`/`description` (invocación implícita) y puedes invocarlas explícitamente con `/skills` o `$nombre`.

| Skill | Cuándo | Qué hace |
|---|---|---|
| **`start`** | Al **empezar** la sesión | Reconstruye contexto (método + `docs/07` + última bitácora), dice la fase y propone el micro-objetivo. Sin que Jay dé contexto. |
| **`log`** | A media sesión / antes de un compact / al **cerrar** | Captura en `docs_sesion/` (`Objetivo · Hecho · Encontrado · Decisiones+porqué · Siguiente`, frontmatter `promoted: false`). Capa cruda, append-only. **No toca docs canónicos.** |
| **`promote`** | Al **consolidar** (fin de sesión/fase) | Destila las bitácoras `promoted: false` a los docs canónicos (07, CLAUDE.md, 01-06, 11…) + vault de Obsidian, y marca `promoted: true`. |

Regla de oro del ciclo: **captura los PORQUÉS** en la bitácora (decisiones, alternativas descartadas, trade-offs). Es lo que da valor y lo que alimenta a `promote`.

## Diferencias respecto al setup de Claude Code (para el agente Codex)

- No tienes la **auto-memoria** de Claude (`~/.claude/.../memory/`). El feedback durable de Jay que allí vivía está **destilado arriba** ("Reglas no negociables" + "Gotchas"). Si aprendes algo nuevo y durable de cómo trabaja Jay, propónlo para AGENTS.md.
- El **vault de Obsidian** (paso 3 de `promote`) sí es filesystem accesible en la Mac de Jay: `/Users/jay/Library/Mobile Documents/com~apple~CloudDocs/Jay_2nd/Jay/`. Actualízalo solo al final de sesión (nunca globees el vault entero).
- Mantén `AGENTS.md` y `CLAUDE.md` **coherentes**: si cambia el método, actualiza ambos.
