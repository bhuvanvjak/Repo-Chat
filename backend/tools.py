"""
tools.py — Agent tools for exploring a git repository.

`build_tools(repo_path, vectorstore)` returns a list of LangChain tools bound to a
specific repo + its vector store, ready to hand to a LangGraph agent.
"""
import os
import subprocess

from langchain_core.tools import tool


def _safe_join(repo_path: str, rel_path: str) -> str:
    """Resolve rel_path under repo_path, refusing to escape the repo root."""
    full = os.path.abspath(os.path.join(repo_path, rel_path))
    if not full.startswith(os.path.abspath(repo_path)):
        raise ValueError("Path escapes repository root.")
    return full


def build_tools(repo_path: str, vectorstore):
    @tool
    def semantic_search(query: str) -> str:
        """Semantic/natural-language search over the repo's code and docs.
        Use this for conceptual questions like 'where is auth handled' or
        'how does the retry logic work'. Returns the most relevant chunks."""
        results = vectorstore.similarity_search(query, k=6)
        if not results:
            return "No relevant results found."
        return "\n\n---\n\n".join(
            f"[{r.metadata.get('source')}]\n{r.page_content}" for r in results
        )

    @tool
    def read_file(relative_path: str) -> str:
        """Read the full contents of a specific file, given its path relative to
        the repo root (e.g. 'src/main.py'). Use this after semantic_search or
        list_directory points you at a file you need full detail on."""
        try:
            full = _safe_join(repo_path, relative_path)
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if len(content) > 20_000:
                return content[:20_000] + "\n\n... [truncated, file is longer]"
            return content
        except Exception as e:
            return f"Error reading file: {e}"

    @tool
    def list_directory(relative_path: str = ".") -> str:
        """List files and subdirectories at a given path relative to the repo root.
        Use '.' for the repo root. Useful for orienting yourself in the repo layout."""
        try:
            full = _safe_join(repo_path, relative_path)
            entries = sorted(os.listdir(full))
            return "\n".join(entries) if entries else "(empty directory)"
        except Exception as e:
            return f"Error listing directory: {e}"

    @tool
    def grep_code(pattern: str, max_results: int = 30) -> str:
        """Search the repo's tracked files for an exact string or regex pattern
        (like grep -rn). Use this for precise lookups, e.g. finding every call site
        of a function name, that semantic_search might miss or blur together."""
        try:
            proc = subprocess.run(
                ["git", "grep", "-n", "-I", "-e", pattern],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            lines = proc.stdout.splitlines()
            if not lines:
                return "No matches found."
            return "\n".join(lines[:max_results])
        except Exception as e:
            return f"Error running grep: {e}"

    @tool
    def git_log(relative_path: str = "", max_entries: int = 15) -> str:
        """Show recent commit history (hash, date, author, subject) for the whole
        repo, or for one file if relative_path is given."""
        try:
            cmd = ["git", "log", f"-{max_entries}", "--pretty=format:%h | %ad | %an | %s", "--date=short"]
            if relative_path:
                cmd += ["--", relative_path]
            proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=15)
            return proc.stdout or "No history found."
        except Exception as e:
            return f"Error running git log: {e}"

    @tool
    def git_blame(relative_path: str) -> str:
        """Show git blame for a file (who last changed each line, and in which
        commit). Use this to answer 'who wrote/changed this' style questions."""
        try:
            proc = subprocess.run(
                ["git", "blame", "--line-porcelain", relative_path],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return proc.stderr.strip() or "Error running git blame."
            # Condense porcelain output to "<short-hash> <author> | <line>"
            lines_out = []
            current = {}
            for line in proc.stdout.splitlines():
                if line.startswith("author "):
                    current["author"] = line[len("author "):]
                elif line.startswith("\t"):
                    lines_out.append(f"{current.get('author', '?')} | {line[1:]}")
            return "\n".join(lines_out[:200])
        except Exception as e:
            return f"Error running git blame: {e}"

    return [semantic_search, read_file, list_directory, grep_code, git_log, git_blame]
