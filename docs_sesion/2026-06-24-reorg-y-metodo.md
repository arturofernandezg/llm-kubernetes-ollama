---
fecha: 2026-06-24
slug: reorg-y-metodo
promoted: true
---

> Entrada inaugural de la bitácora — promovida manualmente en la propia sesión de setup.

## Objetivo
Refrescar el proyecto tras ~1 mes de pausa, reorganizar el repo y diseñar un método de trabajo (bitácora + promoción de docs) para que no se emborrone la documentación.

## Hecho
- **Auditoría completa docs vs código**: el sistema está completo (Fases 0-3 + Mini-Fase 4 + FASE 2), 387 tests, imagen `fd37a5d` desplegada.
- **Reorg del repo (4 fases)**: higiene git (tarballs → `../chromadb-backups/`, borrado `.pytest_cache`/`HISTORIAL.txt`/`package-lock.json`, `.gitignore` ampliado); sync docs (09/06/11); presentación única (eliminados `slides/` y `demo2/`, `demo/demo.html` canónico, números chaos 2026-05-27 en `build_demo.py`); estructura (`sesion_trabajo`→`docs_sesion`, podados scripts throwaway, README+CLAUDE.md actualizados).
- **Método de trabajo (F0)**: 3 skills (`/start`, `/log`, `/promote`) en `.claude/skills/`; `07-roadmap.md` reescrito como fuente única (estado + roadmap + changelog); `09-estado-actual-tutor.md` retirado; sección "Método de trabajo" añadida a `CLAUDE.md`.

## Encontrado / gotchas
- 4 docs competían por "la verdad del estado" (07, 09, 11, CLAUDE.md) → causa raíz del emborronamiento.
- La skill `aiops-vault-session` ya hacía la promoción, pero dependía de la conversación en contexto (sin capa cruda durable) → de ahí la bitácora `/log`. Reemplazada por `/promote`.
- `.claude/` está gitignored → las skills viven locales, no se versionan.
- `slides/` y `demo2/` eran intentos Reveal.js abandonados (`defensa.md §6` lo documenta); backup en `../presentation-archive-20260624.tar.gz`.

## Decisiones + por qué
- **TFM ya evaluado** → foco = presentación de empresa (chapter, 8 o 14 julio 2026), production-readiness; la nota no importa.
- **Roadmap a entrega**: F1 validación cluster → F2 Redis Streams → F3 HPA/CPU → F4 bucle RAG → F5 predicción (stretch) → F6 presentación.
- **Cola = Redis Streams** (reutilizar el Redis existente, cero infra nueva) en vez de NATS.
- **Bitácora por fichero** (no rodante) con frontmatter `promoted`, para tener un boundary limpio de promoción.
- **3 skills** (no 2 ni 1 con modos): separa captura (frecuente, ligera) de promoción (periódica) — encaja con "capturar ahora, promover luego".
- **Cadencia del método**: `/log` es repetible (a media sesión y **antes de un compact**, para no perder contexto al resumirse); `/promote` no tiene que ser cada sesión — lee ficheros (`promoted: false`), así que puede acumular varias bitácoras y funciona incluso tras un compact.

## Siguiente
- **F1 — Validación en cluster**: trazar el pipeline E2E; chaos sobre dependencias propias (Redis/Ollama/ChromaDB) → fail-open; test de concurrencia → ver el dedup de FASE 2; informe de production-readiness.
- Pendiente real (requiere cluster): **Gate 8** — screenshots Grafana (`docs/img/grafana-overview.png` + `grafana-chaos.png`).
- Decisión abierta: esbozar el protocolo de F1 (qué provocar, qué medir, one-liners para cluster) ya, o arrancar limpio la próxima sesión con `/start`.
