"""
ingest_runbooks.py — Seed ChromaDB with the 16 runbooks bundled in the image.

Usage (inside the container or via kubectl exec):
    python ingest_runbooks.py

Env overrides (all optional — defaults match the K8s service names):
    RUNBOOKS_DIR      path inside container  (default: /app/runbooks)
    CHROMADB_HOST     ChromaDB service name  (default: chromadb-svc)
    CHROMADB_PORT     ChromaDB service port  (default: 8000)
    OLLAMA_EMBED_URL  Ollama embeddings URL  (default: http://ollama-svc:11434/api/embeddings)

Exit codes: 0 = all ingested, 1 = one or more errors.
"""

import asyncio
import os
import sys

import httpx

from config import settings
from rag import ingest_all_runbooks

RUNBOOKS_DIR = os.getenv("RUNBOOKS_DIR", "/app/runbooks")


async def run() -> int:
    print(f"Ingesting runbooks from {RUNBOOKS_DIR} ...")
    print(f"  ChromaDB : {settings.chromadb_host}:{settings.chromadb_port}")
    print(f"  Embeddings model : {settings.ollama_embed_model} @ {settings.ollama_embed_url}")

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        stats = await ingest_all_runbooks(RUNBOOKS_DIR, client)

    print(
        f"Done — ingested: {stats['ingested']}, "
        f"errors: {stats['errors']}, "
        f"total: {stats['total']}"
    )
    return 0 if stats["errors"] == 0 else 1


def main():
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
