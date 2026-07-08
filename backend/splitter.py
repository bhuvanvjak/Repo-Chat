"""
splitter.py — Language-aware chunking of repo documents.
"""
from langchain_core.documents import Document
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

# Map file extensions to LangChain's Language enum for syntax-aware splitting.
# Built defensively with getattr() because the exact set of Language members has
# changed across langchain-text-splitters versions (e.g. C/KOTLIN/SOL were added
# later) — an extension whose Language member isn't available in the installed
# version just falls back to the generic splitter instead of crashing on import.
_EXT_TO_LANGUAGE_NAME = {
    ".py": "PYTHON",
    ".js": "JS",
    ".jsx": "JS",
    ".ts": "TS",
    ".tsx": "TS",
    ".java": "JAVA",
    ".go": "GO",
    ".rb": "RUBY",
    ".rs": "RUST",
    ".c": "C",
    ".h": "C",
    ".cpp": "CPP",
    ".hpp": "CPP",
    ".cs": "CSHARP",
    ".php": "PHP",
    ".kt": "KOTLIN",
    ".scala": "SCALA",
    ".swift": "SWIFT",
    ".md": "MARKDOWN",
    ".rst": "RST",
    ".html": "HTML",
    ".sol": "SOL",
}
EXTENSION_LANGUAGE = {
    ext: getattr(Language, name)
    for ext, name in _EXT_TO_LANGUAGE_NAME.items()
    if hasattr(Language, name)
}

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150

_generic_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

_splitter_cache: dict[Language, RecursiveCharacterTextSplitter] = {}


def _splitter_for(language: Language) -> RecursiveCharacterTextSplitter:
    if language not in _splitter_cache:
        _splitter_cache[language] = RecursiveCharacterTextSplitter.from_language(
            language=language, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
        )
    return _splitter_cache[language]


def split_documents(documents: list[Document]) -> list[Document]:
    """Split each document using a language-aware splitter matched to its extension."""
    chunks: list[Document] = []
    for doc in documents:
        ext = doc.metadata.get("extension", "")
        language = EXTENSION_LANGUAGE.get(ext)
        splitter = _splitter_for(language) if language else _generic_splitter
        for i, chunk in enumerate(splitter.split_documents([doc])):
            chunk.metadata["chunk_index"] = i
            chunks.append(chunk)
    return chunks


if __name__ == "__main__":
    import sys

    from loader import load_repo

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    _, docs = load_repo(target)
    chunks = split_documents(docs)
    print(f"{len(docs)} files -> {len(chunks)} chunks")
