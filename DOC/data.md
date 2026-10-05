# Data reference

`ril_interactions` contains one row per turn. The table schema is in
[database setup](database.md).

| Fields | Meaning |
| --- | --- |
| `ts`, `duration_ms` | Arrival time in UTC and completed turn duration. |
| `instance`, `user_id`, `turn_id` | Cat process, user, and local or Guardrails turn identifier. |
| `outcome` | `generated`, `fast_reply`, or `incomplete`. |
| `question`, `llm_answer`, `delivered` | User input, generated answer, and answer actually sent. |
| `guard_present`, `*_verdict`, `other_plugin_reply` | Optional Guardrails and fast-reply metadata. |
| `recall_count`, `recall_top_score`, `recall_sources` | Recall count, best score, and source metadata. |
| `tools_used`, `tool_input`, `tool_output` | Tool names and optional input/output records. |

Questions and answers are limited to 20,000 characters. Longer values end with
`[cut: N characters in total]` before they enter the queue.

`recall_sources` is a JSON array of `{id, source, score}` and never contains document
text. `tool_input` and `tool_output` are JSON arrays with at most ten objects. Tool
values are stored only when their setting is enabled and are limited by `tool_text_limit`.

A shortened tool value includes `"cut":true` and its original `"chars"` count.

Rows can contain personal data. Limit table and backup access, define a lawful retention
period, and use `retention_days`. Tool input and output are off by default because the
plugin cannot assess their content.
