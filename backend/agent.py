"""
agent.py — Build a LangGraph ReAct agent (chat reasoning via the Groq API) with
per-conversation memory, wired up with the repo-exploration tools.
"""
from groq import BadRequestError
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from tools import build_tools

# Groq-hosted Llama models occasionally emit a malformed tool call (400
# tool_use_failed) when several tools are bound, especially under a multi-tool
# ReAct setup like this one. It's stochastic — resampling the same turn almost
# always succeeds — so retry a couple of times before surfacing the error.
MAX_TOOL_CALL_RETRIES = 2

SYSTEM_PROMPT = """You are GitReader, an expert assistant that helps developers understand a
specific git repository. You have tools to semantically search the codebase, read files,
list directories, grep for exact matches, and inspect git history/blame.

Guidelines:
- Prefer semantic_search first to find relevant areas, then read_file for full context.
- Use grep_code for precise symbol/string lookups (exact function names, imports, etc).
- Use git_log / git_blame when the user asks about history, authorship, or recent changes.
- Always ground answers in what the tools return — don't guess at code you haven't read.
- Cite file paths (and line ranges when relevant) in your answers.
- If you're not sure a file/symbol exists, check with list_directory or grep_code first.
"""


def build_agent(repo_path: str, vectorstore, chat_model: str = "openai/gpt-oss-20b"):
    """Return a compiled LangGraph agent. Use a distinct thread_id per conversation
    when invoking it so histories don't mix.

    Requires the GROQ_API_KEY environment variable to be set."""
    llm = ChatGroq(model=chat_model, temperature=0.1)
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
    config = {"configurable": {"thread_id": thread_id}}
    for attempt in range(MAX_TOOL_CALL_RETRIES + 1):
        try:
            result = agent.invoke({"messages": [HumanMessage(content=question)]}, config=config)
            return result["messages"][-1].content
        except BadRequestError as e:
            is_tool_use_failure = (
                getattr(e, "body", None) and e.body.get("error", {}).get("code") == "tool_use_failed"
            )
            if not is_tool_use_failure or attempt == MAX_TOOL_CALL_RETRIES:
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
    config = {"configurable": {"thread_id": thread_id}}
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
            return
        except BadRequestError as e:
            is_tool_use_failure = (
                getattr(e, "body", None) and e.body.get("error", {}).get("code") == "tool_use_failed"
            )
            if any_tokens_sent or not is_tool_use_failure or attempt == MAX_TOOL_CALL_RETRIES:
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
