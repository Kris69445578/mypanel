import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("PANEL_DB", "/var/lib/mypanel/panel.db")
_lock = threading.Lock()


@contextmanager
def db():
    with _lock:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


# columns added for egg-based servers (added to old databases automatically)
SERVER_COLUMNS = {
    "egg": "TEXT",  # egg id, NULL for old template-based servers
    "variables": "TEXT",  # JSON {ENV_VAR: value}
    "memory_mb": "INTEGER",
    "cpus": "REAL",
    "port": "INTEGER",
    "state": "TEXT",  # installing | install_failed | ready
}


def init():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL,
              pw_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'user');
            CREATE TABLE IF NOT EXISTS servers(
              name TEXT PRIMARY KEY,
              owner_id INTEGER,
              image TEXT NOT NULL,
              created INTEGER NOT NULL);
            """
        )
        have = {r["name"] for r in c.execute("PRAGMA table_info(servers)")}
        for col, typ in SERVER_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE servers ADD COLUMN {col} {typ}")
