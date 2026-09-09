SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'teacher')),
    teacher_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (teacher_id) REFERENCES teachers(id)
);

CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    teacher_id INTEGER NOT NULL,
    purchased_lessons INTEGER NOT NULL CHECK (purchased_lessons >= 0),
    current_balance INTEGER NOT NULL CHECK (current_balance >= 0),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
    FOREIGN KEY (teacher_id) REFERENCES teachers(id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_package_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    planned_time TEXT NOT NULL,
    reminder_enabled INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_package_id) REFERENCES course_packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lesson_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    lesson_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'completed', 'skipped')),
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(schedule_id, lesson_date),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lesson_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_instance_id INTEGER NOT NULL,
    course_package_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('complete', 'skip', 'undo_complete', 'undo_skip', 'adjust')),
    delta_lessons INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    actor_user_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lesson_instance_id) REFERENCES lesson_instances(id),
    FOREIGN KEY (course_package_id) REFERENCES course_packages(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS lesson_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_package_id INTEGER NOT NULL,
    lesson_record_id INTEGER,
    delta_lessons INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_package_id) REFERENCES course_packages(id),
    FOREIGN KEY (lesson_record_id) REFERENCES lesson_records(id)
);

CREATE TABLE IF NOT EXISTS email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_email TEXT NOT NULL,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('student', 'teacher')),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'disabled')),
    error TEXT,
    related_type TEXT,
    related_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balance_alerts (
    course_package_id INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    notified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (course_package_id, threshold),
    FOREIGN KEY (course_package_id) REFERENCES course_packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""



MIGRATIONS = {
    "email_logs": {
        "attempts": "ALTER TABLE email_logs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "ALTER TABLE email_logs ADD COLUMN next_attempt_at TEXT",
        "last_attempt_at": "ALTER TABLE email_logs ADD COLUMN last_attempt_at TEXT",
    },
}


def migrate_schema(conn):
    """给已经在跑的旧数据库补上新增字段，不影响已有数据。"""
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, statement in columns.items():
            if column not in existing:
                conn.execute(statement)
