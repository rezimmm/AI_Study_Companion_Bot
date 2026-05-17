import sqlite3


# ---------- DB CONNECTION ----------
def get_db():
    return sqlite3.connect("analytics.db")


# ---------- INITIALIZE DB ----------
def init_db():
    conn = get_db()
    c = conn.cursor()

    # Users Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        joined TEXT,
        usage_count INTEGER DEFAULT 0
    )
    """)

    # AI Latency Logs
    c.execute("""
    CREATE TABLE IF NOT EXISTS latency_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_ms INTEGER,
        ts INTEGER
    )
    """)

    # Uptime Logs
    c.execute("""
    CREATE TABLE IF NOT EXISTS uptime_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER
    )
    """)

    # Broadcast History
    c.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT,
        sent_at INTEGER
    )
    """)

    # System Settings Table (AUTO MAINTENANCE SUPPORT)
    c.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        id INTEGER PRIMARY KEY,
        maintenance_mode INTEGER DEFAULT 0,
        bot_enabled INTEGER DEFAULT 1,
        theme TEXT DEFAULT 'dark',
        last_reason TEXT DEFAULT 'Normal',
        last_triggered INTEGER DEFAULT 0
    )
    """)

    # Ensure ONE ROW always exists
    c.execute("INSERT OR IGNORE INTO system_settings(id) VALUES (1)")

    conn.commit()
    conn.close()


# Run DB init at import
init_db()


# ---------- USER FUNCTIONS ----------
def add_user(user_id, name):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
    INSERT OR IGNORE INTO users(user_id,name,joined)
    VALUES (?, ?, datetime('now'))
    """, (user_id, name))
    conn.commit()
    conn.close()


def increment_usage(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id=?",
              (user_id,))
    conn.commit()
    conn.close()


def get_users():
    conn = get_db()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM users").fetchall()
    conn.close()
    return rows


# ---------- SYSTEM SETTINGS ----------
def get_settings():
    conn = get_db()
    c = conn.cursor()

    row = c.execute("""
    SELECT maintenance_mode, bot_enabled, theme,
           COALESCE(last_reason,'Normal'),
           COALESCE(last_triggered,0)
    FROM system_settings WHERE id = 1
    """).fetchone()

    conn.close()

    return {
        "maintenance_mode": bool(row[0]),
        "bot_enabled": bool(row[1]),
        "theme": row[2],
        "reason": row[3],
        "last_time": int(row[4])
    }


def update_settings(maintenance=None, bot=None, theme=None, reason=None):
    conn = get_db()
    c = conn.cursor()

    if maintenance is not None:
        c.execute("""
        UPDATE system_settings
        SET maintenance_mode = ?,
            last_reason = COALESCE(?, last_reason),
            last_triggered = strftime('%s','now')
        WHERE id = 1
        """, (1 if maintenance else 0, reason))

    if bot is not None:
        c.execute("UPDATE system_settings SET bot_enabled=? WHERE id=1",
                  (1 if bot else 0,))

    if theme:
        c.execute("UPDATE system_settings SET theme=? WHERE id=1",
                  (theme,))

    conn.commit()
    conn.close()


# ---------- PDF UPLOAD LOG ----------
def log_pdf_upload(name):
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS upload_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT,
        ts INTEGER
    )
    """)

    c.execute(
        "INSERT INTO upload_log(file_name, ts) VALUES (?, strftime('%s','now'))",
        (name,)
    )

    conn.commit()
    conn.close()
