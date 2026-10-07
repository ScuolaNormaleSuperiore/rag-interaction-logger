# Operations and analysis

## Connection check

Saving plugin settings starts an asynchronous check. The Cat log reports either
`Database check passed` or `Database check failed at <stage>`. The stages are `connect`,
`tls`, `table`, and `write`.

| Code | Likely cause |
| --- | --- |
| `1045` | Wrong user or password. |
| `1044`, `1142` | Missing privilege. |
| `1049` | Database does not exist. |
| `1054` | Table schema is missing a column. |
| `1146` | Table does not exist and `create_table` is off. |
| `2003` | Server cannot be reached. |
| `2026` | TLS is required but unavailable. |

## Failure behaviour

Hooks only enqueue events; a background thread performs all database work. A slow or
unavailable database does not affect a user turn. Events can be lost when the queue is
full, writes fail, or the process stops. The logger reports the start of loss and later
recovery; it does not retry writes.

The queue is created when the plugin is activated, so a new `queue_size` applies only after
the plugin is reactivated or the Cat restarts. The other settings apply from the next event.

## Example queries

Query results can contain questions, answers, user identifiers, and other personal
data. Anonymise results before sharing them in tickets, logs, or screenshots.

```sql
-- guard verdicts by stage
SELECT 'input' AS stage, input_verdict AS verdict, COUNT(*) AS n
FROM ril_interactions WHERE input_verdict IS NOT NULL GROUP BY input_verdict
UNION ALL
SELECT 'output', output_verdict, COUNT(*)
FROM ril_interactions WHERE output_verdict IS NOT NULL GROUP BY output_verdict;

-- share of generated answers with no recalled document
SELECT ROUND(100 * AVG(recall_count = 0), 1) AS empty_recall_pct
FROM ril_interactions WHERE outcome = 'generated';

-- turns using a named tool
SELECT ts, question, tools_used FROM ril_interactions
WHERE FIND_IN_SET('service_status', tools_used) > 0 ORDER BY ts DESC;

-- incomplete turns and turns without Guardrails
SELECT outcome, guard_present, COUNT(*) FROM ril_interactions
WHERE outcome = 'incomplete' OR NOT guard_present GROUP BY outcome, guard_present;
```
