import sqlite3
import os
import datetime

DB_PATH = os.getenv("DB_PATH", "alerts.db")

# For 'recurring' alerts: minimum time between two consecutive notifications
# for the same alert, so it doesn't spam every poll cycle while the price
# hovers around the threshold.
RECURRING_COOLDOWN_MINUTES = int(os.getenv("RECURRING_COOLDOWN_MINUTES", "15"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _column_names(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            condition TEXT NOT NULL CHECK(condition IN ('above', 'below')),
            target_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            triggered_at TEXT,
            triggered_price REAL
        )
        """
    )
    # Lightweight migration for databases created before "recurring" alerts existed.
    existing = _column_names(conn, "alerts")
    if "mode" not in existing:
        conn.execute("ALTER TABLE alerts ADD COLUMN mode TEXT NOT NULL DEFAULT 'one_time'")
    if "last_triggered_at" not in existing:
        conn.execute("ALTER TABLE alerts ADD COLUMN last_triggered_at TEXT")
    conn.commit()
    conn.close()


def add_alert(
    user_id: int, exchange: str, symbol: str, condition: str, target_price: float, mode: str = "one_time"
) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO alerts (user_id, exchange, symbol, condition, target_price, mode, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (user_id, exchange, symbol, condition, target_price, mode, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    alert_id = cur.lastrowid
    conn.close()
    return alert_id


def get_active_alerts(user_id: int | None = None) -> list[dict]:
    conn = get_conn()
    if user_id is None:
        rows = conn.execute("SELECT * FROM alerts WHERE status = 'active'").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE status = 'active' AND user_id = ?", (user_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_alert(alert_id: int, user_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def mark_triggered(alert_id: int, price: float) -> None:
    """One-time alert fired: deactivate it."""
    conn = get_conn()
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE alerts SET status = 'triggered', triggered_at = ?, triggered_price = ?, last_triggered_at = ? "
        "WHERE id = ?",
        (now, price, now, alert_id),
    )
    conn.commit()
    conn.close()


def record_recurring_trigger(alert_id: int, price: float) -> None:
    """Recurring alert fired: stays active, just remember when for the cooldown."""
    conn = get_conn()
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE alerts SET last_triggered_at = ?, triggered_at = ?, triggered_price = ? WHERE id = ?",
        (now, now, price, alert_id),
    )
    conn.commit()
    conn.close()


def is_in_cooldown(alert: dict) -> bool:
    last = alert.get("last_triggered_at")
    if not last:
        return False
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except ValueError:
        return False
    elapsed = datetime.datetime.utcnow() - last_dt
    return elapsed < datetime.timedelta(minutes=RECURRING_COOLDOWN_MINUTES)
