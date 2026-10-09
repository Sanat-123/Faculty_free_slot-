"""
LLM assistant layer
===================

Lets people talk to the timetable chatbot in ANY wording, like a
general-purpose AI assistant, while every fact still comes from the loaded
timetable.

How it works
------------
    user message  (+ recent chat history)
          │
          ▼
    Claude  ── understands the wording, resolves "he / that / tomorrow",
          │    splits multi-part questions, makes small talk
          │
          ├── tool: ask_timetable("<standalone question>")
          │         └─► SmartQueryEngine  (data-driven, reads the loaded DB)
          │
          ▼
    natural-language reply, grounded in the tool results

The rule-based SmartQueryEngine stays the single source of truth, so the
assistant cannot invent teachers, rooms or free slots: it is told to use
only what the tool returns.  Nothing about a particular college is
hard-coded here; the dataset overview in the system prompt is generated
from whatever timetable is loaded.

Configuration (environment variables)
-------------------------------------
    ANTHROPIC_API_KEY   required to switch the AI layer on
    FACULTY_LLM_MODEL   optional, default "claude-sonnet-5-5"
    FACULTY_LLM_OFF=1   optional, force the plain rule-based engine

Without a key (or if the SDK is missing) `LLMAssistant.available()` is
False and callers keep using the rule-based engine.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "claude-sonnet-5-5"

MAX_TOOL_ROUNDS = 6          # safety cap on tool calls per message
MAX_TOOL_RESULT_CHARS = 14000
MAX_HISTORY_MESSAGES = 10
MAX_HISTORY_CHARS = 1500

TOOLS = [
    {
        "name": "ask_timetable",
        "description": (
            "Ask the timetable database ONE self-contained question and get "
            "the exact answer (Markdown, possibly with tables) computed from "
            "the loaded timetable. Use it for every fact about teachers, "
            "classes, subjects, rooms, labs, days, slots, free/busy status, "
            "workloads and timetables. The question must make sense on its "
            "own: always include full names, the day and the slot, in the "
            "forms listed in the system prompt. Never pass pronouns such as "
            "'he' or 'that class'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A standalone timetable question.",
                }
            },
            "required": ["question"],
        },
    }
]


class LLMAssistant:
    """Claude front end for a SmartQueryEngine."""

    def __init__(self, engine, client=None, model=None):

        self.engine = engine
        self.model = model or os.environ.get("FACULTY_LLM_MODEL", DEFAULT_MODEL)
        self._client = client
        self.system_prompt = self._build_system_prompt()

    # ------------------------------------------------------------------
    # availability
    # ------------------------------------------------------------------

    @staticmethod
    def available() -> bool:
        """True when an API key is set and the SDK can be imported."""

        if os.environ.get("FACULTY_LLM_OFF") == "1":
            return False

        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False

        try:
            import anthropic  # noqa: F401
        except Exception:
            return False

        return True

    @property
    def client(self):

        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=45.0, max_retries=1)

        return self._client

    # ------------------------------------------------------------------
    # prompt (generated from the loaded data)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:

        overview = self._dataset_overview()

        try:
            examples = "\n".join(f"- {q}" for q in self.engine.examples()[:8])
        except Exception:
            examples = "- Who is free on Monday slot 3?"

        return f"""You are a friendly, knowledgeable timetable assistant. \
You talk like a helpful colleague, not like a database.

## Loaded timetable (summary)
{overview}

## How to work
1. For ANY fact about this timetable, call the `ask_timetable` tool. \
Never answer timetable facts from memory or guess; the tool reads the real data.
2. Turn the person's message into one or more standalone questions. \
Resolve pronouns and follow-ups ("what about slot 4?", "is he free?", \
"and tomorrow?") using the chat history, and always write full names, \
day and slot into the question. Split multi-part requests into several calls.
3. If the tool says it could not map or find something, rephrase ONCE using \
the plain forms below, or use the closest names it suggested. If it still \
fails, say so honestly and suggest what you can do; do not make things up.
4. Answer in the person's language and tone (English, Hinglish, etc.). \
Lead with the direct answer in a sentence or two, then add helpful detail.
5. When the tool returns a table or a list, include it in your reply in full \
(do not drop rows or invent extra ones). You may add a short summary \
or point out what matters (for example the earliest free slot).
6. Small talk and general questions that have nothing to do with the timetable \
can be answered briefly and naturally; gently mention what you can help with.
7. You cannot change the timetable. If asked to, explain that you can only \
read it, and offer to find suitable free slots or substitutes instead.

## Plain question forms the data engine understands well
{examples}
"""

    def _dataset_overview(self) -> str:

        m = self.engine.model

        def sample(items, n=6):
            items = list(items)
            more = f", … ({len(items)} total)" if len(items) > n else ""
            return ", ".join(str(x) for x in items[:n]) + more

        slot_lines = []

        for s in m.slots:
            label = ""
            try:
                label = m.slot_label(s) or ""
            except Exception:
                pass
            slot_lines.append(f"{s}" + (f" ({label})" if label else ""))

        return "\n".join([
            f"- Days: {', '.join(d.capitalize() for d in m.days)}",
            f"- Slots: {', '.join(slot_lines)}",
            f"- Faculty ({len(m.faculty)}): {sample(m.faculty)}",
            f"- Classes ({len(m.classes)}): {sample(m.classes)}",
            f"- Rooms ({len(m.rooms)}): {sample(m.rooms)}",
            f"- Subjects ({len(m.subjects)}): {sample(m.subjects)}",
        ])

    # ------------------------------------------------------------------
    # tool execution
    # ------------------------------------------------------------------

    def _run_tool(self, name, tool_input, asked):

        if name != "ask_timetable":
            return f"Unknown tool: {name}"

        question = str((tool_input or {}).get("question", "")).strip()

        if not question:
            return "Empty question."

        asked.append(question)

        # each tool call is a standalone question: don't let the engine's
        # own follow-up memory leak the previous one into it
        try:
            self.engine.reset_context()
        except Exception:
            pass

        text = self.engine.answer(question)

        if text is None:
            try:
                text = self.engine.fallback_text()
            except Exception:
                text = "I couldn't map that to a timetable question."

        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = (
                text[:MAX_TOOL_RESULT_CHARS]
                + "\n\n…(result truncated; ask a narrower question for the rest)"
            )

        return text

    # ------------------------------------------------------------------
    # conversation
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_history(history):
        """Alternating user/assistant text turns, starting with a user turn."""

        turns = []

        for item in (history or [])[-MAX_HISTORY_MESSAGES:]:

            role = item.get("role")
            text = str(item.get("content") or "").strip()

            if role not in ("user", "assistant") or not text:
                continue

            text = text[:MAX_HISTORY_CHARS]

            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + text
            else:
                turns.append({"role": role, "content": text})

        while turns and turns[0]["role"] != "user":
            turns.pop(0)

        return turns

    def answer(self, message, history=None):
        """
        Returns {"text": str, "queries": [questions sent to the engine]}.
        Raises on API errors so the caller can fall back to the rule engine.
        """

        messages = self._clean_history(history)

        if messages and messages[-1]["role"] == "user":
            # current message goes after an assistant turn
            messages.pop()

        messages.append({"role": "user", "content": str(message)})

        asked = []

        for _ in range(MAX_TOOL_ROUNDS + 1):

            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=self.system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":

                text = "".join(
                    block.text for block in response.content
                    if getattr(block, "type", "") == "text"
                ).strip()

                if not text:
                    raise RuntimeError("empty reply from the model")

                return {"text": text, "queries": asked}

            assistant_blocks = []
            results = []

            for block in response.content:

                kind = getattr(block, "type", "")

                if kind == "text":
                    assistant_blocks.append(
                        {"type": "text", "text": block.text}
                    )

                elif kind == "tool_use":
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": self._run_tool(
                            block.name, block.input, asked
                        ),
                    })

            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": results})

        raise RuntimeError("too many tool rounds")