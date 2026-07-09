"""
agent.py — Build a LangGraph ReAct agent (chat reasoning via the Groq API) with
per-conversation memory, wired up with the repo-exploration tools.
"""
from groq import APIError, BadRequestError
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from tools import build_tools

# LangGraph's default recursion_limit (25, ~12 tool-call round trips). Tried
# lowering this to fail faster/cheaper on genuinely stuck loops, but that backfired
# hard: openai/gpt-oss-20b routinely explores 5-6 files (list_directory, several
# read_files) even for simple overview questions before it settles down and
# answers, and a lower ceiling cut those off mid-exploration far more often than
# it caught actual runaway loops. Sticking with the default — the real fix for
# runaway loops is the system prompt telling the model to stop exploring and
# answer, not a hard ceiling low enough to break normal multi-file questions.
RECURSION_LIMIT = 25

# Multi-tool Groq agents occasionally hit transient tool-calling glitches that
# resampling the same turn almost always fixes:
#   - BadRequestError (HTTP 400, code "tool_use_failed"): the model emitted a
#     malformed tool call the server couldn't parse.
#   - Plain APIError ("Failed to parse tool call arguments as JSON"): a client-side
#     failure to reassemble fragmented tool-call-argument deltas during streaming.
#     This is a *sibling* of BadRequestError, not a subclass, and has no .body —
#     so it needs its own check rather than falling under the same isinstance.
# Both are worth a couple of silent retries before surfacing to the user.
MAX_TOOL_CALL_RETRIES = 2


def _is_retryable_tool_glitch(e: Exception) -> bool:
    if isinstance(e, BadRequestError):
        body = getattr(e, "body", None) or {}
        return body.get("error", {}).get("code") == "tool_use_failed"
    return isinstance(e, APIError) and "Failed to parse tool call arguments" in str(e)


DID_NOT_CONVERGE_MESSAGE = (
    "I wasn't able to converge on an answer to that within a reasonable number of tool "
    "calls — it may not be something answerable from the repo's contents alone. Try "
    "rephrasing, or pointing me at a specific file."
)

SYSTEM_PROMPT = """You are GitReader, an expert assistant that helps developers understand a
specific git repository. You have tools to semantically search the codebase, read files,
list directories, grep for exact matches, and inspect git history/blame.

Guidelines:
- Prefer semantic_search first to find relevant areas, then read_file for full context.
- Use grep_code for precise symbol/string lookups (exact function names, imports, etc).
- Use git_log / git_blame when the user asks about history, authorship, or recent changes.
- Always ground answers in what the tools return — don't guess at code you haven't read.
- If you're not sure a file/symbol exists, check with list_directory or grep_code first.
- Some questions (opinions, intent, "why" questions about decisions not documented in
  the repo) can't be resolved by more tool calls. After a few searches, if you aren't
  converging on an answer, say what you found and what you couldn't determine — don't
  keep calling tools hoping something new turns up.
- For general "what is this project / what does this do" questions: list_directory
  plus reading ONE or TWO key files (README if present, otherwise package.json /
  pyproject.toml / the main entry point) is enough for a good answer. Don't work
  through the file tree exhaustively — summarize from what you have as soon as you
  can describe the project's purpose, not after you've read every file.

Citing files: every time you mention a specific file, cite it as a markdown link
using the `repo://` scheme with the path relative to the repo root — never a plain
github.com URL (you don't know the branch or host). Add a line range with `#L<start>-L<end>`
when you're citing a specific location, omit it when referring to the whole file.
Examples: `[server.py](repo://server.py)` or `[server.py:12-34](repo://server.py#L12-L34)`.
"""


def build_agent(
    repo_path: str, vectorstore, chat_model: str = "openai/gpt-oss-20b", groq_api_key: str | None = None
):
    """Return a compiled LangGraph agent. Use a distinct thread_id per conversation
    when invoking it so histories don't mix.

    `groq_api_key` lets each caller (e.g. each browser extension user) supply their
    own key per request; if omitted, falls back to the GROQ_API_KEY environment
    variable (e.g. for the CLI in __main__ below, using a local .env).

    Note: ChatGroq only reads GROQ_API_KEY as a *default_factory*, which pydantic
    only invokes when a field is omitted entirely — explicitly passing api_key=None
    would suppress that fallback rather than trigger it, so the kwarg is only
    included when a key was actually supplied."""
    extra_kwargs = {"api_key": groq_api_key} if groq_api_key else {}
    llm = ChatGroq(model=chat_model, temperature=0.1, **extra_kwargs)
    tools = build_tools(repo_path, vectorstore)
    checkpointer = MemorySaver()
    # The prebuilt agent's system-prompt kwarg has been renamed across langgraph
    # versions ("prompt" in current releases, "state_modifier" in some older ones).
    # Try both so this keeps working regardless of exactly which version is installed.
    try:
        agent = create_react_agent(llm, tools=tools, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)
    except TypeError:
        agent = create_react_agent(
            llm, tools=tools, state_modifier=SYSTEM_PROMPT, checkpointer=checkpointer
        )
    return agent


def ask(agent, thread_id: str, question: str) -> str:
    """Send a question to the agent within a given conversation thread and return
    the final text answer."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    for attempt in range(MAX_TOOL_CALL_RETRIES + 1):
        try:
            result = agent.invoke({"messages": [HumanMessage(content=question)]}, config=config)
            return result["messages"][-1].content
        except GraphRecursionError:
            return DID_NOT_CONVERGE_MESSAGE
        except APIError as e:
            if not _is_retryable_tool_glitch(e) or attempt == MAX_TOOL_CALL_RETRIES:
                raise


def ask_stream(agent, thread_id: str, question: str):
    """Like `ask`, but yields incremental events as the answer is generated instead
    of blocking for the full response:

      {"type": "tool", "tool": "<tool_name>"}   — the agent started using a tool
      {"type": "token", "text": "<chunk>"}      — a piece of the final answer text
      {"type": "error", "detail": "<message>"}  — unrecoverable failure

    Only retries the tool_use_failed Groq quirk (see `ask`) if no answer tokens
    have been streamed to the caller yet — once real output has been sent, a
    later failure can't be silently retried without duplicating content, so it's
    surfaced as an error event instead.
    """
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    seen_tool_calls: set[str] = set()
    any_tokens_sent = False

    for attempt in range(MAX_TOOL_CALL_RETRIES + 1):
        try:
            for chunk, metadata in agent.stream(
                {"messages": [HumanMessage(content=question)]}, config=config, stream_mode="messages"
            ):
                if metadata.get("langgraph_node") == "agent":
                    for call in getattr(chunk, "tool_call_chunks", None) or []:
                        if call["id"] not in seen_tool_calls:
                            seen_tool_calls.add(call["id"])
                            yield {"type": "tool", "tool": call["name"]}
                    if chunk.content:
                        any_tokens_sent = True
                        yield {"type": "token", "text": chunk.content}
            # Unlike `.invoke()`, `.stream(stream_mode="messages")` does not raise
            # GraphRecursionError when the step limit is hit mid-run — it just stops
            # yielding. If nothing was ever said, that's what happened; say so rather
            # than silently ending the turn with no reply at all.
            if not any_tokens_sent:
                yield {"type": "token", "text": DID_NOT_CONVERGE_MESSAGE}
            return
        except GraphRecursionError:
            if any_tokens_sent:
                yield {
                    "type": "error",
                    "detail": "Stopped after too many tool calls without converging.",
                }
            else:
                yield {"type": "token", "text": DID_NOT_CONVERGE_MESSAGE}
            return
        except APIError as e:
            if any_tokens_sent or not _is_retryable_tool_glitch(e) or attempt == MAX_TOOL_CALL_RETRIES:
                yield {"type": "error", "detail": str(e)}
                return


if __name__ == "__main__":
    import sys

    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # can't encode arbitrary Unicode in file contents or model output; force
    # UTF-8 with a safe fallback instead of crashing mid-answer.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import load_dotenv

    load_dotenv()  # picks up GROQ_API_KEY from a .env file in backend/, if present

    from loader import load_repo
    from splitter import split_documents
    from vectorstore import build_or_load_vectorstore

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    path, docs = load_repo(target)
    chunks = split_documents(docs)
    store = build_or_load_vectorstore(path, chunks)
    agent = build_agent(path, store)

    print(f"GitReader ready for {path}. Type 'exit' to quit.")
    thread_id = "cli-session"
    while True:
        q = input("\nyou> ").strip()
        if q.lower() in {"exit", "quit"}:
            break
        if not q:
            continue
        try:
            print("\ngitreader>", ask(agent, thread_id, q))
        except Exception as e:
            print(f"\ngitreader> Error: {e}\n(You can just try asking again.)")
