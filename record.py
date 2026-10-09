"""Pure interaction-record state, independent from Cheshire Cat and MySQL."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime


TOOLS_USED_WIDTH = 255
TOOL_TEXT_LIMIT = 1000
TOOL_TEXT_LIMIT_MIN = 100
TOOL_TEXT_LIMIT_MAX = 10000
TOOL_CALLS_MAX = 10
RECALL_SOURCES_MAX = 20
RECALL_SOURCE_LIMIT = 200
RECALL_URL_LIMIT = 500
RECALL_TITLE_LIMIT = 200
RECALL_LABEL_LIMIT = 64
# The column is TEXT (65 535 bytes) and a strict server rejects the whole row when a value
# is longer, so the JSON is kept under this size by dropping the last documents.
RECALL_SOURCES_BYTES = 60_000
RECALL_ID_LIMIT = 64


@dataclass(slots=True)
class InteractionRecord:
    """A turn observed by the logger before it is written asynchronously."""

    ts: datetime
    started_ns: int
    instance: str
    user_id: str
    question: str | None
    local_turn_id: str
    guard_turn_id_at_start: str | None = None
    turn_id: str | None = None
    guard_present: bool = False
    llm_answer: str | None = None
    delivered: str | None = None
    input_verdict: str | None = None
    output_verdict: str | None = None
    other_plugin_reply: bool | None = None
    recall_count: int | None = None
    recall_top_score: float | None = None
    tools_used: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    recall_sources: str | None = None
    duration_ms: int | None = None
    outcome: str = "incomplete"


def start_record(
    *,
    ts: datetime,
    started_ns: int,
    instance: str,
    user_id: str,
    question: str | None,
    guard_turn_id_at_start: str | None,
    local_turn_id: str,
) -> InteractionRecord:
    """Create the incomplete record that H1 stores in working memory.

    The guard has not run yet in H1, so any guard turn id already present
    belongs to an earlier turn; it is kept to recognise a stale attribute later.
    """
    return InteractionRecord(
        ts=ts,
        started_ns=started_ns,
        instance=instance,
        user_id=user_id,
        question=question,
        local_turn_id=local_turn_id,
        guard_turn_id_at_start=guard_turn_id_at_start or None,
        turn_id=local_turn_id,
    )


def resolve_turn(record: InteractionRecord, guard_turn_id: str | None) -> InteractionRecord:
    """Decide whether the guard handled this turn and fix the turn id.

    The guard attribute persists in working memory, so an id equal to the one
    seen in H1 was left by an earlier turn, or by a guard that is not running.
    """
    present = bool(guard_turn_id) and guard_turn_id != record.guard_turn_id_at_start
    return replace(
        record,
        guard_present=present,
        turn_id=guard_turn_id if present else record.local_turn_id,
    )


def capture_generated(
    record: InteractionRecord,
    text: str | None,
    declarative: list | None,
    steps=None,
    tool_limit: int = TOOL_TEXT_LIMIT,
    include_tool_input: bool = True,
    include_tool_output: bool = True,
) -> InteractionRecord:
    """Store the LLM answer, the recall and the tools that ran (H3).

    `declarative` is `why.memory["declarative"]` and `steps` is
    `why.intermediate_steps`; without them the matching columns stay empty.
    `tool_limit` is the longest tool input or output kept, in characters. A tool
    text that is not going to be saved (`include_tool_input` or
    `include_tool_output` false) is not even converted to text: the conversion of a
    large structure would be paid on every turn for nothing.
    """
    calls = tool_calls(steps, include_tool_input, include_tool_output)
    found = {
        "llm_answer": text,
        "tools_used": join_tools([name.replace(",", "_") for name, _, _ in calls]),
        "tool_input": tool_texts_json(calls, "input", tool_limit) if include_tool_input else None,
        "tool_output": tool_texts_json(calls, "output", tool_limit) if include_tool_output else None,
        "recall_sources": recall_sources_json(declarative),
    }
    if declarative is None:
        return replace(record, **found)
    scores = [
        entry["score"]
        for entry in declarative
        if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float))
    ]
    return replace(
        record,
        recall_count=len(declarative),
        recall_top_score=max(scores) if scores else None,
        **found,
    )


def tool_text_limit_from(settings: dict) -> int:
    """Read `tool_text_limit` from the settings, kept inside its allowed range."""
    try:
        value = int(settings.get("tool_text_limit"))
    except (TypeError, ValueError, AttributeError):
        return TOOL_TEXT_LIMIT
    return max(TOOL_TEXT_LIMIT_MIN, min(TOOL_TEXT_LIMIT_MAX, value))


def tool_calls(steps, with_input: bool = True, with_output: bool = True) -> list[tuple[str, str, str]]:
    """Return `(name, input, output)` for each tool or form that ran, in order.

    Each step of `why.intermediate_steps` is `((name, input), output)`. Anything
    that is not a step with a name is ignored. A text that is not asked for
    (`with_input` or `with_output` false) comes back empty and is never converted.
    """
    if not isinstance(steps, (list, tuple)):
        return []
    calls = []
    for step in steps:
        parts = _step_parts(step, with_input, with_output)
        if parts is not None:
            calls.append(parts)
    return calls


def tool_names(steps) -> list[str]:
    """Return only the names, with a comma replaced so the list stays parsable."""
    return [name.replace(",", "_") for name, _, _ in tool_calls(steps, False, False)]


def join_tools(names: list[str], width: int = TOOLS_USED_WIDTH) -> str | None:
    """Join the names with commas, keeping only whole names that fit the column."""
    kept: list[str] = []
    length = 0
    for name in names:
        extra = len(name) + (1 if kept else 0)
        if length + extra > width:
            break
        kept.append(name)
        length += extra
    return ",".join(kept) or None


def tool_texts_json(
    calls: list[tuple[str, str, str]], field: str, limit: int = TOOL_TEXT_LIMIT
) -> str | None:
    """Return the `input` or `output` of each call as a JSON array text, or None.

    One object per call, `{"tool": name, field: text}`, at most `TOOL_CALLS_MAX`.
    A text longer than `limit` is cut *before* the array is built, so the result
    is always valid JSON, and the object also carries `"cut":true` and the
    original length as `"chars"`.
    """
    if not calls or field not in ("input", "output"):
        return None
    column = 1 if field == "input" else 2
    items = []
    for call in calls[:TOOL_CALLS_MAX]:
        text = call[column]
        item = {"tool": call[0], field: text[:limit]}
        if len(text) > limit:
            item["cut"] = True
            item["chars"] = len(text)
        items.append(item)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def tool_input_json(calls: list[tuple[str, str, str]], limit: int = TOOL_TEXT_LIMIT) -> str | None:
    return tool_texts_json(calls, "input", limit)


def tool_output_json(calls: list[tuple[str, str, str]], limit: int = TOOL_TEXT_LIMIT) -> str | None:
    return tool_texts_json(calls, "output", limit)


def recall_sources_json(declarative) -> str | None:
    """Return what identifies each recalled document as a JSON array text.

    The document text is never read. Per document: `id`, `type`, `source` (the file
    name or URL the Cat stored), and from its metadata `origin`, `title`, `wp_id` and
    `url` (for example from the WordPress importer), then `score`. At most
    `RECALL_SOURCES_MAX` entries; `source`, `url` and `title` are cut to their limit
    (with `"cut"`, `"url_cut"`, `"title_cut"` true) before the array is built. If the
    text would pass `RECALL_SOURCES_BYTES`, the last documents are dropped. None when
    nothing was recalled.
    """
    if not isinstance(declarative, (list, tuple)):
        return None
    items = []
    for entry in declarative:
        item = _recall_item(entry)
        if item is not None:
            items.append(item)
        if len(items) == RECALL_SOURCES_MAX:
            break
    while items:
        text = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= RECALL_SOURCES_BYTES:
            return text
        items.pop()
    return None


def finalize_fast_reply(
    record: InteractionRecord, delivered: str | None, input_verdict: str | None, now_ns: int
) -> InteractionRecord:
    """Close a turn answered on `fast_reply`; only a present guard has a verdict."""
    verdict = input_verdict if record.guard_present else None
    return replace(
        record,
        outcome="fast_reply",
        llm_answer=None,
        delivered=delivered,
        input_verdict=verdict,
        other_plugin_reply=verdict is None if record.guard_present else None,
        duration_ms=_duration_ms(record, now_ns),
    )


def finalize_generated(
    record: InteractionRecord, delivered: str | None, output_verdict: str | None, now_ns: int
) -> InteractionRecord:
    """Close a turn whose answer was generated and then delivered (H4)."""
    return replace(
        record,
        outcome="generated",
        delivered=delivered,
        output_verdict=output_verdict if record.guard_present else None,
        other_plugin_reply=False if record.guard_present else None,
        duration_ms=_duration_ms(record, now_ns),
    )


def extract_reply_text(value) -> str | None:
    """Return the text of a `fast_reply` result that ends the turn, else None.

    Mirrors the core: a message object carries `text`, a dict carries `output`.
    """
    if isinstance(value, dict):
        return str(value["output"]) if "output" in value else None
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else None


def _step_parts(step, with_input: bool = True, with_output: bool = True) -> tuple[str, str, str] | None:
    try:
        action = step[0]
        if isinstance(action, (list, tuple)):
            name, raw = action[0], (action[1] if len(action) > 1 else "")
        elif isinstance(action, dict):
            name, raw = action.get("tool"), action.get("tool_input", "")
        else:
            name, raw = getattr(action, "tool", None), getattr(action, "tool_input", "")
        output = step[1] if len(step) > 1 else ""
    except (TypeError, IndexError, KeyError):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip(), _as_text(raw) if with_input else "", _as_text(output) if with_output else ""


def _recall_item(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    item: dict = {}
    identifier = entry.get("id")
    if identifier is not None and str(identifier):
        item["id"] = str(identifier)[:RECALL_ID_LIMIT]
    metadata = entry.get("metadata")
    source = metadata.get("source") if isinstance(metadata, dict) else None
    cut = isinstance(source, str) and len(source) > RECALL_SOURCE_LIMIT
    if isinstance(source, str) and source:
        item["source"] = source[:RECALL_SOURCE_LIMIT]
    if not item:
        return None
    meta = metadata if isinstance(metadata, dict) else {}
    for key, value in (("type", entry.get("type")), ("origin", meta.get("origin")), ("wp_id", meta.get("wp_id"))):
        label = _label(value)
        if label:
            item[key] = label
    url, title = meta.get("url"), meta.get("title")
    if isinstance(url, str) and url:
        item["url"] = url[:RECALL_URL_LIMIT]
    if isinstance(title, str) and title:
        item["title"] = title[:RECALL_TITLE_LIMIT]
    score = entry.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        item["score"] = round(float(score), 6)
    if cut:
        item["cut"] = True
    if isinstance(url, str) and len(url) > RECALL_URL_LIMIT:
        item["url_cut"] = True
    if isinstance(title, str) and len(title) > RECALL_TITLE_LIMIT:
        item["title_cut"] = True
    return item


def _label(value) -> str | None:
    """A short identifier such as a type, an origin or a post id: text or a number."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value)[:RECALL_LABEL_LIMIT] or None


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _duration_ms(record: InteractionRecord, now_ns: int) -> int:
    return max(0, (now_ns - record.started_ns) // 1_000_000)
