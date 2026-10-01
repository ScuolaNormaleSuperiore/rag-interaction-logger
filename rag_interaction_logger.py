"""Cheshire Cat hook adapters for RAG Interaction Logger.

The first implementation deliberately records no data yet. It establishes the
four observer-only hook points and priorities from DEV/AGENTS/PROJECT.md; later work adds
record assembly, the queue worker and MySQL persistence without changing their
public wiring.
"""

from cat.mad_hatter.decorators import hook


@hook("fast_reply", priority=100)
def start_interaction_record(message, cat):
    """H1: start a record before other fast-reply hooks run."""
    return None


@hook("fast_reply", priority=-100)
def finalize_fast_reply_record(message, cat):
    """H2: observe a final fast reply after every other hook."""
    return None


@hook("before_cat_sends_message", priority=100)
def capture_generated_answer(message, cat):
    """H3: observe the LLM answer before any output rewrite."""
    return None


@hook("before_cat_sends_message", priority=-100)
def finalize_generated_record(message, cat):
    """H4: observe the delivered answer after every output rewrite."""
    return None
