"""
loader.py — Load a git repository (local path or remote URL) into LangChain Documents.
"""
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from git import GitCommandError, Repo
from langchain_core.documents import Document

# Extensions we consider "text/code" and worth indexing.
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
    ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".sql", ".proto", ".graphql", ".dockerfile",
}

# Directories we always skip regardless of .gitignore.
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".mypy_cache"}

MAX_FILE_BYTES = 500_000  # skip huge generated/binary-ish files


def is_url(path_or_url: str) -> bool:
    return path_or_url.startswith("http://") or path_or_url.startswith("https://") or path_or_url.startswith("git@")


def load_repo(path_or_url: str, workdir: str | None = None) -> tuple[str, list[Document]]:
    """
    Clone (if URL) or read (if local path) a repo, and return (local_repo_path, documents).

    Each Document has metadata: {"source": <relative path>, "abspath": <absolute path>}.
    """
    if is_url(path_or_url):
        workdir = workdir or tempfile.mkdtemp(prefix="gitreader_")
        repo_path = os.path.join(workdir, "repo")
        if os.path.exists(repo_path):
            shutil.rmtree(repo_path)
        try:
            Repo.clone_from(path_or_url, repo_path, depth=1)
        except GitCommandError:
            # Some networks (antivirus/proxy doing SSL inspection) return a
            # valid-looking HTTP/2 response whose body git can't actually read,
            # failing with "Could not read from remote repository" even though
            # the repo exists and is reachable. Retry once forcing HTTP/1.1,
            # which sidesteps that class of middlebox issue.
            if os.path.exists(repo_path):
                shutil.rmtree(repo_path)
            Repo.clone_from(
                path_or_url,
                repo_path,
                depth=1,
                multi_options=["-c", "http.version=HTTP/1.1"],
                allow_unsafe_options=True,
            )
    else:
        repo_path = os.path.abspath(path_or_url)
        if not os.path.isdir(repo_path):
            raise ValueError(f"Local path does not exist or is not a directory: {repo_path}")

    documents = _walk_repo(repo_path)
    return repo_path, documents


def _read_one(repo_path_obj: Path, fpath: Path) -> Document | None:
    try:
        if fpath.stat().st_size > MAX_FILE_BYTES:
            return None
        content = fpath.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return None

    if not content.strip():
        return None

    rel_path = str(fpath.relative_to(repo_path_obj))
    return Document(
        page_content=content,
        metadata={"source": rel_path, "abspath": str(fpath), "extension": fpath.suffix.lower()},
    )


def _walk_repo(repo_path: str) -> list[Document]:
    repo_path_obj = Path(repo_path)
    candidates: list[Path] = []

    for root, dirs, files in os.walk(repo_path):
        # prune skip dirs in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]

        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() in TEXT_EXTENSIONS:
                candidates.append(fpath)

    # File reads are I/O-bound, so a thread pool gives a large speedup on repos
    # with many files without the overhead of multiprocessing.
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = pool.map(lambda fpath: _read_one(repo_path_obj, fpath), candidates)

    return [doc for doc in results if doc is not None]


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    path, documents = load_repo(target)
    print(f"Loaded {len(documents)} files from {path}")
    for d in documents[:5]:
        print(" -", d.metadata["source"])
