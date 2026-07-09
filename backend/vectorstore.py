"""
vectorstore.py — Build/load a local Chroma vector store using local HuggingFace
(sentence-transformers) embeddings.

Embeddings run fully locally on CPU via sentence-transformers — no API key or
network call needed, so this stays free/local even though chat reasoning now
goes through the Groq API (see agent.py).
"""
import hashlib
import logging
import os
from functools import lru_cache

# Must be set before chromadb is imported (directly or via langchain_chroma) to
# disable its posthog telemetry.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

# Belt-and-suspenders: on some chromadb/posthog version combos, chromadb calls a
# legacy posthog API regardless of the setting above; it's caught internally and
# just logged as an error ("capture() takes 1 positional argument but 3 were
# given"), harmless but noisy. Silence that specific logger.
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

CHROMA_ROOT = os.path.join(os.path.expanduser("~"), ".gitreader", "chroma")


def _best_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _collection_name(repo_path: str) -> str:
    """Stable, filesystem-safe collection name derived from the repo path."""
    digest = hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:16]
    return f"repo_{digest}"


@lru_cache(maxsize=4)
def _get_embeddings(embedding_model: str) -> HuggingFaceEmbeddings:
    # Loading sentence-transformers weights from disk into memory takes several
    # seconds; caching by model name means only the *first* /load in a running
    # backend pays that cost; every subsequent load (even of a different repo)
    # reuses the already-loaded model.
    return HuggingFaceEmbeddings(
        model_name=embedding_model,
        model_kwargs={"device": _best_device()},
        # A bigger batch size cuts per-call Python/model overhead — the default of 32
        # leaves a lot of CPU (or GPU) throughput on the table for repos with hundreds
        # of chunks.
        encode_kwargs={"batch_size": 128},
    )


def get_or_create_store(
    repo_path: str,
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    force_rebuild: bool = False,
) -> tuple[Chroma, bool]:
    """Open (or create) the persistent Chroma collection for this repo. Returns
    (store, is_new) — is_new is True when the caller still needs to embed and add
    chunks (a fresh collection, or force_rebuild was requested)."""
    os.makedirs(CHROMA_ROOT, exist_ok=True)
    embeddings = _get_embeddings(embedding_model)
    collection = _collection_name(repo_path)
    persist_dir = os.path.join(CHROMA_ROOT, collection)

    is_new = force_rebuild or not os.path.exists(persist_dir)

    store = Chroma(
        collection_name=collection,
        embedding_function=embeddings,
        persist_directory=persist_dir,
    )
    return store, is_new


def add_documents_in_batches(store: Chroma, chunks: list[Document], batch_size: int = 64):
    """Embed and add chunks in batches, yielding (num_done, num_total) after each
    batch. Splitting the single add_documents() call into batches — rather than
    embedding everything in one shot — is what lets a caller report live progress
    for large repos instead of blocking silently for the whole embedding phase."""
    total = len(chunks)
    for start in range(0, total, batch_size):
        batch = chunks[start : start + batch_size]
        # Chroma dedupes by id; use source+chunk_index as a stable id.
        ids = [
            f"{c.metadata.get('source', 'unknown')}::{c.metadata.get('chunk_index', 0)}"
            for c in batch
        ]
        store.add_documents(batch, ids=ids)
        yield min(start + batch_size, total), total


def build_or_load_vectorstore(
    repo_path: str,
    chunks: list[Document] | None = None,
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    force_rebuild: bool = False,
) -> Chroma:
    """
    Build a new persistent Chroma collection for this repo, or load the existing one.

    If `chunks` is provided and (force_rebuild or the collection is empty/new),
    the chunks are embedded and inserted. For progress reporting during embedding,
    use `get_or_create_store` + `add_documents_in_batches` directly instead.
    """
    store, is_new = get_or_create_store(repo_path, embedding_model, force_rebuild)
    if is_new and chunks:
        for _ in add_documents_in_batches(store, chunks):
            pass
    return store


if __name__ == "__main__":
    import sys

    from loader import load_repo
    from splitter import split_documents

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    path, docs = load_repo(target)
    chunks = split_documents(docs)
    store = build_or_load_vectorstore(path, chunks, force_rebuild=True)
    results = store.similarity_search("main entry point", k=3)
    for r in results:
        print(r.metadata["source"])
