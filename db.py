"""Database access for data-monitoring.

The DB user (`monitoring_ro`) is **read-only** on the whole monitored schema and
may only INSERT/UPDATE the single history table `T_WC_DATA_MONITORING_SNAPSHOT`
(idempotent daily upsert - see the UNIQUE key `UK_SNAPSHOT_DAY`). It holds no
DELETE grant: the snapshot history is never pruned, it is the whole point.

Connection parameters come from the environment (same pattern as the sibling
crawler repos): DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME.
"""
import os

SNAPSHOT_TABLE = "T_WC_DATA_MONITORING_SNAPSHOT"


def get_connection():
    """Open a pymysql connection with a dict cursor (lazy import so --sample
    mode never needs the driver installed)."""
    import pymysql
    import pymysql.cursors
    return pymysql.connect(
        host=os.environ.get("DB_HOST", ""),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", ""),
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )


def scalar(conn, sql):
    """Run a single-value SELECT; return the first column of the first row."""
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if not row:
        return None
    return list(row.values())[0]


def fetch_pairs(conn, sql):
    """Run a two-column (label, value) SELECT; return a list of (label, value)."""
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for r in rows:
        vals = list(r.values())
        out.append((vals[0], vals[1]))
    return out


def previous_done(conn, slug, source_db, table_name, metric_key, before_date):
    """DONE_COUNT of the most recent earlier snapshot, for the daily-rate delta."""
    sql = (
        f"SELECT DONE_COUNT FROM {SNAPSHOT_TABLE} "
        "WHERE REPORT_SLUG=%s AND SOURCE_DB=%s AND TABLE_NAME=%s AND METRIC_KEY=%s "
        "AND DAT_CREAT < %s AND DELETED=0 ORDER BY DAT_CREAT DESC LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (slug, source_db, table_name, metric_key, before_date))
        row = cur.fetchone()
    return row["DONE_COUNT"] if row else None


def pct_history(conn, slug, metric_key):
    """(DAT_CREAT, PCT) history of a metric, for the completion sparkline."""
    sql = (
        f"SELECT DAT_CREAT, PCT FROM {SNAPSHOT_TABLE} "
        "WHERE REPORT_SLUG=%s AND METRIC_KEY=%s AND DELETED=0 "
        "ORDER BY DAT_CREAT ASC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (slug, metric_key))
        rows = cur.fetchall()
    return [(r["DAT_CREAT"], r["PCT"]) for r in rows]


def server_variables(conn, prefix):
    """All T_WC_SERVER_VARIABLE rows whose VAR_NAME starts with `prefix`.

    Returns {VAR_NAME: (VAR_VALUE, TIM_UPDATED)}. Used by the `pipeline` metric
    kind to read a batch job's per-step status/timestamps (the crawlers write one
    row per step: <prefix>step<code>status / startedat / finishedat).
    """
    sql = (
        "SELECT VAR_NAME, VAR_VALUE, TIM_UPDATED FROM T_WC_SERVER_VARIABLE "
        "WHERE VAR_NAME LIKE %s AND (DELETED = 0 OR DELETED IS NULL)"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (prefix + "%",))
        rows = cur.fetchall()
    return {r["VAR_NAME"]: (r["VAR_VALUE"], r["TIM_UPDATED"]) for r in rows}


def done_history(conn, slug, metric_key):
    """(DAT_CREAT, DONE_COUNT) history of a metric, for the alert_zero count trend."""
    sql = (
        f"SELECT DAT_CREAT, DONE_COUNT FROM {SNAPSHOT_TABLE} "
        "WHERE REPORT_SLUG=%s AND METRIC_KEY=%s AND DELETED=0 "
        "ORDER BY DAT_CREAT ASC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (slug, metric_key))
        rows = cur.fetchall()
    return [(r["DAT_CREAT"], r["DONE_COUNT"]) for r in rows]


def upsert_snapshot(conn, row):
    """Idempotent one-row-per-(day, source, table, metric) write.

    Needs INSERT + UPDATE on the snapshot table (the UPDATE branch fires on a
    same-day re-run). DAT_CREAT / SOURCE_DB / TABLE_NAME / METRIC_KEY form the
    unique key, so they are never part of the UPDATE set.
    """
    cols = [
        "REPORT_SLUG", "SOURCE_DB", "TABLE_NAME", "METRIC_KEY",
        "DONE_COUNT", "EXPECTED_COUNT", "PCT", "DAILY_RATE",
        "DESCRIPTION", "LONG_DESC", "DELETED", "DISPLAY_ORDER",
        "ID_CREATOR", "DAT_CREAT", "ID_OWNER", "TIM_UPDATED", "ID_USER_UPDATED",
    ]
    key_cols = {"DAT_CREAT", "SOURCE_DB", "TABLE_NAME", "METRIC_KEY"}
    placeholders = ",".join(["%s"] * len(cols))
    updates = ",".join(f"{c}=VALUES({c})" for c in cols if c not in key_cols)
    sql = (
        f"INSERT INTO {SNAPSHOT_TABLE} ({','.join(cols)}) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, [row.get(c) for c in cols])
    conn.commit()
