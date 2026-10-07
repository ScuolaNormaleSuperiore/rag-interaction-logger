"""Cheshire Cat hook adapters for RAG Interaction Logger.

The hooks only observe: each reads what it needs, keeps the turn in the logger's
own working-memory attribute and hands plain values to the writer queue. They
return None, change nothing they receive and never let an error out.
"""

import socket
import time
from datetime import datetime, timezone
from uuid import uuid4

from cat.log import log
from cat.mad_hatter.decorators import hook, plugin

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, so the
# relative import is the one that works at runtime; the absolute fallback is for
# the tests, which put the plugin folder on the path.
try:
    from .record import (
        TOOL_TEXT_LIMIT,
        capture_generated,
        extract_reply_text,
        finalize_fast_reply,
        finalize_generated,
        resolve_turn,
        start_record,
    )
    from .writer import Writer
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from record import (
        TOOL_TEXT_LIMIT,
        capture_generated,
        extract_reply_text,
        finalize_fast_reply,
        finalize_generated,
        resolve_turn,
        start_record,
    )
    from writer import Writer


# Where rag-guardrails leaves its turn id and verdict. They are read with
# getattr and never imported: the guard is optional enrichment.
GUARD_TURN_ID_ATTRIBUTE = "ict_guard_turn_id"
GUARD_VERDICT_ATTRIBUTE = "ict_guard_verdict"
# This plugin's own working-memory attribute.
RECORD_ATTRIBUTE = "ril_record"

INSTANCE = socket.gethostname()

_state = {"plugin": None}
_reported = set()


def _load_settings() -> dict:
    plugin_object = _state["plugin"]
    return plugin_object.load_settings() if plugin_object is not None else {}


_writer = Writer(load_settings=_load_settings, log=log)


@plugin
def activated(plugin_object):
    """Start the writer; the Cat may call this again without a deactivation.

    An error here would make the Cat fail the activation, so it is reported and
    the logger stays idle instead.
    """
    try:
        _state["plugin"] = plugin_object
        _writer.start()
    except Exception as error:
        _hook_failed("activated", error)


@plugin
def deactivated(plugin_object):
    """Stop the writer; rows still queued are lost.

    An error here would stop the Cat half-way through deactivating the plugin.
    """
    try:
        _writer.stop()
    except Exception as error:
        _hook_failed("deactivated", error)


@hook("fast_reply", priority=100)
def start_interaction_record(message, cat):
    """H1: start the record before any other fast-reply hook and queue its insert."""
    try:
        memory = cat.working_memory
        _remember_plugin(cat)
        record = start_record(
            ts=datetime.now(timezone.utc),
            started_ns=time.monotonic_ns(),
            instance=INSTANCE,
            user_id=str(cat.user_id),
            question=_text_of(getattr(memory, "user_message_json", None)),
            guard_turn_id_at_start=_guard_value(memory, GUARD_TURN_ID_ATTRIBUTE),
            local_turn_id=uuid4().hex,
        )
        setattr(memory, RECORD_ATTRIBUTE, record)
        _writer.submit_start(record)
    except Exception as error:
        _hook_failed("fast_reply start", error)
    return None


@hook("fast_reply", priority=-100)
def finalize_fast_reply_record(message, cat):
    """H2: close the turn when the final fast-reply value is a reply."""
    record = None
    try:
        reply = extract_reply_text(message)
        memory = cat.working_memory
        record = getattr(memory, RECORD_ATTRIBUTE, None)
        if reply is None or record is None:
            return None
        record = resolve_turn(record, _guard_value(memory, GUARD_TURN_ID_ATTRIBUTE))
        record = finalize_fast_reply(
            record, reply, _guard_value(memory, GUARD_VERDICT_ATTRIBUTE), time.monotonic_ns()
        )
        _writer.submit_finish(record)
        setattr(memory, RECORD_ATTRIBUTE, None)
    except Exception as error:
        _hook_failed("fast_reply finish", error, record)
    return None


@hook("before_cat_sends_message", priority=100)
def capture_generated_answer(message, cat):
    """H3: keep the LLM answer and the recall summary before any rewrite."""
    record = None
    try:
        memory = cat.working_memory
        record = getattr(memory, RECORD_ATTRIBUTE, None)
        if record is None:
            return None
        setattr(
            memory,
            RECORD_ATTRIBUTE,
            capture_generated(
                record,
                _text_of(message),
                _declarative_recall(message),
                _intermediate_steps(message),
                getattr(_writer, "tool_text_limit", TOOL_TEXT_LIMIT),
                getattr(_writer, "keeps_tool_input", True),
                getattr(_writer, "keeps_tool_output", True),
            ),
        )
    except Exception as error:
        _hook_failed("before_cat_sends_message capture", error, record)
    return None


@hook("before_cat_sends_message", priority=-100)
def finalize_generated_record(message, cat):
    """H4: close the turn with the answer actually delivered."""
    record = None
    try:
        memory = cat.working_memory
        record = getattr(memory, RECORD_ATTRIBUTE, None)
        if record is None:
            return None
        record = resolve_turn(record, _guard_value(memory, GUARD_TURN_ID_ATTRIBUTE))
        record = finalize_generated(
            record,
            _text_of(message),
            _guard_value(memory, GUARD_VERDICT_ATTRIBUTE),
            time.monotonic_ns(),
        )
        _writer.submit_finish(record)
        setattr(memory, RECORD_ATTRIBUTE, None)
    except Exception as error:
        _hook_failed("before_cat_sends_message finish", error, record)
    return None


def _remember_plugin(cat) -> None:
    """Fall back to the plugin object when `activated` did not provide it."""
    if _state["plugin"] is None:
        try:
            _state["plugin"] = cat.mad_hatter.get_plugin()
        except Exception as error:
            _hook_failed("plugin lookup", error)


def _guard_value(memory, name: str):
    """Read an attribute the guard may have left; absence is a normal state."""
    value = getattr(memory, name, None)
    return None if value is None else str(value)


def _text_of(value) -> str | None:
    text = value.get("text") if isinstance(value, dict) else getattr(value, "text", None)
    return text if isinstance(text, str) else None


def _declarative_recall(message) -> list | None:
    why = getattr(message, "why", None)
    memory = why.get("memory") if isinstance(why, dict) else getattr(why, "memory", None)
    recall = memory.get("declarative") if isinstance(memory, dict) else None
    return recall if isinstance(recall, list) else None


def _intermediate_steps(message) -> list | None:
    why = getattr(message, "why", None)
    steps = why.get("intermediate_steps") if isinstance(why, dict) else getattr(why, "intermediate_steps", None)
    return steps if isinstance(steps, (list, tuple)) else None


def _hook_failed(name: str, error: Exception, record=None) -> None:
    """Log a hook failure once per kind, with no text from the error."""
    kind = (name, type(error).__name__)
    if kind in _reported:
        return
    _reported.add(kind)
    turn = f" turn={record.turn_id}" if record is not None else ""
    try:
        log.warning(f"RAG Interaction Logger: {name} hook failed ({kind[1]}){turn}")
    except Exception:
        pass  # a broken logger must not turn a logging failure into a hook failure
