"""Real retriever stage (LLD 05). Drop-in replacement for FakeRetriever."""
from .evaluation import EvalItem, EvalReport, evaluate
from .memory_store import InMemoryChunkStore
from .retriever import HybridRetriever
from .selection import reciprocal_rank_fusion
from .types import (ChunkStore, Embedder, Hit, IsolationViolation, MissingFilterError, QueryRewriter, Reranker,
                    RetrievalConfig, RetrievalUnavailable, SearchFilter, StoredChunk)

__all__ = ["ChunkStore", "Embedder", "EvalItem", "EvalReport", "Hit", "HybridRetriever", "InMemoryChunkStore",
           "IsolationViolation", "MissingFilterError", "QueryRewriter", "Reranker", "RetrievalConfig",
           "RetrievalUnavailable", "SearchFilter", "StoredChunk", "evaluate", "reciprocal_rank_fusion"]

try:  # optional dependency: `pip install qdrant-client`
    from .qdrant_store import Bm25SparseEncoder, QdrantChunkStore
    __all__ += ["Bm25SparseEncoder", "QdrantChunkStore"]
except ImportError:  # pragma: no cover
    pass
