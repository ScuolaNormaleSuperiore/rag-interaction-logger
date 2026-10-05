# Database setup

The logger needs an external MySQL or MariaDB database. The DBA creates the database
and users; the plugin never creates either.

## Logger database and user

Create a dedicated database and user. The privileges are limited to that database.
`CREATE` is needed only when `create_table` is on.

```sql
CREATE DATABASE `rag-interaction-logger-db`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'ril_logger'@'%' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT CREATE, INSERT, UPDATE, SELECT, DELETE
  ON `rag-interaction-logger-db`.* TO 'ril_logger'@'%';
```

With `create_table=on` (the default), the plugin creates `ril_interactions` on its
first successful connection. If the DBA manages the schema, set `create_table=off` and
run the following before enabling the plugin:

```sql
CREATE TABLE IF NOT EXISTS ril_interactions (
 id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 ts DATETIME(3) NOT NULL,
 duration_ms INT UNSIGNED NULL,
 instance VARCHAR(255) NOT NULL,
 user_id VARCHAR(255) NOT NULL,
 turn_id VARCHAR(32) NULL,
 outcome ENUM('generated','fast_reply','incomplete') NOT NULL,
 question MEDIUMTEXT NULL,
 llm_answer MEDIUMTEXT NULL,
 delivered MEDIUMTEXT NULL,
 guard_present BOOLEAN NOT NULL,
 input_verdict VARCHAR(64) NULL,
 output_verdict VARCHAR(64) NULL,
 other_plugin_reply BOOLEAN NULL,
 recall_count SMALLINT UNSIGNED NULL,
 recall_top_score FLOAT NULL,
 tools_used VARCHAR(255) NULL,
 tool_input MEDIUMTEXT NULL,
 tool_output MEDIUMTEXT NULL,
 recall_sources TEXT NULL,
 KEY idx_ts (ts),
 KEY idx_user_ts (user_id, ts),
 KEY idx_input_verdict (input_verdict),
 KEY idx_output_verdict (output_verdict)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## Optional WordPress monitor

Only create this read-only user when using the optional WordPress monitor:

```sql
CREATE USER 'ril_monitor'@'%' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT SELECT ON `rag-interaction-logger-db`.ril_interactions TO 'ril_monitor'@'%';
```

## Authentication and TLS

Both engines must report `mysql_native_password` for the logger user. MariaDB uses it
by default. MySQL 8.4 must start with `mysql_native_password=ON` and
`authentication_policy='mysql_native_password,,'`; MySQL 9 is unsupported.

Keep `REQUIRE SSL` and `db_require_ssl=on` outside local development. For a local
server without TLS, remove `REQUIRE SSL` and set `db_require_ssl=off`.
