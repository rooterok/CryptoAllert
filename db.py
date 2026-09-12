import sqlite3
import os
import datetime

DB_PATH = os.getenv("DB_PATH", "alerts.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()
    conn.close()


def add_alert(user_id: int, exchange: str, symbol: str, condition: str, target_price: float) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO alerts (user_id, exchange, symbol, condition, target_price, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (user_id, exchange, symbol, condition, target_price, datetime.datetime.utcnow().isoformat()),
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
    conn = get_conn()
    conn.execute(
        "UPDATE alerts SET status = 'triggered', triggered_at = ?, triggered_price = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), price, alert_id),
    )
    conn.commit()
    conn.close()
