"""Seed the demo course notes into a Qdrant server so `uvicorn studybuddy.api.dev:app` can search them.

    docker run -p 6333:6333 qdrant/qdrant
    export QDRANT_URL=http://localhost:6333
    python -m studybuddy.api.dev_seed_qdrant
    uvicorn studybuddy.api.dev:app          # same QDRANT_URL in its environment

Idempotent: running it again replaces the same points. Reads QDRANT_URL, QDRANT_API_KEY and QDRANT_COLLECTION from the
environment only (nothing is printed except counts).
"""
from __future__ import annotations

import asyncio
import os

from ..retrieval import QdrantChunkStore
from ..retrieval.fakes import HashingEmbedder
from .dev import DEMO_SYNONYMS, QDRANT_DEMO_COLLECTION, demo_chunks


async def seed(store: QdrantChunkStore, embedder: HashingEmbedder) -> int:
    await store.ensure_collection()
    chunks = demo_chunks()
    await store.publish(chunks, embedder)
    return len(chunks)


async def main() -> None:
    url = os.environ.get("QDRANT_URL")
    if not url:
        raise SystemExit("set QDRANT_URL (for example http://localhost:6333)")
    embedder = HashingEmbedder(synonyms=DEMO_SYNONYMS)
    store = QdrantChunkStore.from_url(url, os.environ.get("QDRANT_COLLECTION", QDRANT_DEMO_COLLECTION),
                                      dim=embedder.dim, api_key=os.environ.get("QDRANT_API_KEY"))
    print(f"seeded {await seed(store, embedder)} chunks into {store.collection}")


if __name__ == "__main__":
    asyncio.run(main())
