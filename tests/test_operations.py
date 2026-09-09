from datetime import datetime, timedelta
import sqlite3
import subprocess

import pytest

from app.emailer import email_queue_summary, queue_email
from app.models import SCHEMA_SQL
from app.services import init_schema, seed_data
from scripts.send_reminders import run_reminders
from scripts import backup_sqlite


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / 'test.sqlite3')
    connection.row_factory = sqlite3.Row
    init_schema(connection, SCHEMA_SQL)
    seed_data(connection)
    connection.commit()
    yield connection
    connection.close()


def test_reminder_matches_configured_minute_once_per_day(conn):
    conn.execute("UPDATE settings SET value = '18:35' WHERE key = 'reminder_time'")
    assert not run_reminders(conn, datetime(2026, 9, 6, 18, 34))
    assert conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0] == 0
    assert run_reminders(conn, datetime(2026, 9, 6, 18, 35))
    count = conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0]
    assert count == 4  # Monday: two lessons, each with student + teacher.
    conn.commit()
    assert not run_reminders(conn, datetime(2026, 9, 6, 18, 35, 50))
    conn.execute("UPDATE settings SET value = '19:00' WHERE key = 'reminder_time'")
    assert not run_reminders(conn, datetime(2026, 9, 6, 19))
    assert conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0] == count
    assert run_reminders(conn, datetime(2026, 9, 13, 19))
    assert conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0] == count * 2


def test_reminder_failure_rolls_back_daily_marker_and_partial_queue(conn, monkeypatch):
    from scripts import send_reminders
    original = send_reminders.create_reminder_logs

    def fail(connection, weekday):
        original(connection, weekday)
        raise RuntimeError('database failure')

    monkeypatch.setattr(send_reminders, 'create_reminder_logs', fail)
    with pytest.raises(RuntimeError), conn:
        run_reminders(conn, datetime(2026, 9, 6, 19))
    assert conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0] == 0
    monkeypatch.setattr(send_reminders, 'create_reminder_logs', original)
    assert run_reminders(conn, datetime(2026, 9, 6, 19))


def test_summary_empty(conn):
    assert email_queue_summary(conn) == {'pending': 0, 'failed': 0, 'last_success': None, 'error': None}


def add_log(conn, status, when, error=None):
    queue_email(conn, 'student@example.com', 'student', 'subject', 'body', 'test', 1)
    conn.execute('UPDATE email_logs SET status = ?, last_attempt_at = ?, error = ? WHERE id = last_insert_rowid()',
                 (status, when.isoformat(timespec='seconds'), error))


def test_summary_all_successful(conn):
    now = datetime(2026, 9, 9, 12)
    add_log(conn, 'sent', now - timedelta(hours=2))
    add_log(conn, 'sent', now - timedelta(hours=1))
    assert email_queue_summary(conn, now) == {'pending': 0, 'failed': 0, 'last_success': '2026-09-09T11:00:00', 'error': None}


def test_summary_failed_window_and_pending_retry(conn):
    now = datetime(2026, 9, 9, 12)
    add_log(conn, 'failed', now - timedelta(hours=25), 'old')
    add_log(conn, 'failed', now - timedelta(hours=24), 'boundary')
    add_log(conn, 'failed', now - timedelta(hours=1), '<authentication failed>')
    add_log(conn, 'pending', now, 'retrying')
    add_log(conn, 'disabled', now)
    summary = email_queue_summary(conn, now)
    assert summary == {'pending': 1, 'failed': 2, 'last_success': None, 'error': '<authentication failed>'}


@pytest.mark.parametrize('role', ['admin', 'teacher'])
def test_today_failure_notice_respects_role(conn, monkeypatch, role):
    from contextlib import contextmanager
    from starlette.requests import Request
    from app import main

    @contextmanager
    def session():
        yield conn

    monkeypatch.setattr(main, 'db_session', session)
    user = dict(conn.execute('SELECT * FROM users WHERE role = ? LIMIT 1', (role,)).fetchone())
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'app': main.app})
    assert '邮件发送失败' not in main.today(request, user=user).body.decode()
    add_log(conn, 'failed', datetime.now(), '<smtp error>')
    body = main.today(request, user=user).body.decode()
    assert '邮件发送失败' in body
    if role == 'admin':
        assert '查看邮件状态和错误' in body
        settings_body = main.settings(request, user=user).body.decode()
        assert '24 小时内失败 1 封' in settings_body
        assert '&lt;smtp error&gt;' in settings_body
    else:
        assert '请联系管理员' in body
        assert 'href="/settings"' not in body
        with pytest.raises(main.HTTPException) as exc:
            main.settings(request, user=user)
        assert exc.value.status_code == 403


@pytest.mark.parametrize('value', ['25:00', '19:60', '', '7:00'])
def test_settings_reject_invalid_reminder_time(value):
    from app.main import save_settings, HTTPException
    with pytest.raises(HTTPException) as exc:
        save_settings(reminder_time=value, low_balance_thresholds='5', user={'role': 'admin'})
    assert exc.value.status_code == 422


def test_backup_without_remote_preserves_database(tmp_path, monkeypatch):
    source = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE sample (value TEXT)')
        db.execute("INSERT INTO sample VALUES ('lesson balance')")
    monkeypatch.setenv('DATABASE_URL', str(source))
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.delenv('BACKUP_RCLONE_REMOTE', raising=False)
    monkeypatch.setattr(backup_sqlite.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected upload'))
    target = backup_sqlite.main()
    with sqlite3.connect(target) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert restored.execute('SELECT value FROM sample').fetchone()[0] == 'lesson balance'


@pytest.mark.parametrize('failure', [FileNotFoundError('rclone missing'), subprocess.CalledProcessError(1, 'rclone'), subprocess.TimeoutExpired('rclone', 300)])
def test_upload_failure_keeps_local_backup(tmp_path, monkeypatch, caplog, failure):
    source = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE sample (value TEXT)')
    monkeypatch.setenv('DATABASE_URL', str(source))
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setenv('BACKUP_RCLONE_REMOTE', 'remote:backups')

    def fail(args, **kwargs):
        with sqlite3.connect(args[2]) as restored:
            assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert restored.execute('SELECT COUNT(*) FROM sample').fetchone()[0] == 0
        raise failure

    monkeypatch.setattr(backup_sqlite.subprocess, 'run', fail)
    target = backup_sqlite.main()
    assert target.exists()
    assert '异地备份上传失败，本地备份已保留' in caplog.text


def test_upload_calls_rclone_with_target_and_remote(tmp_path, monkeypatch):
    target = tmp_path / 'backup.sqlite3'
    monkeypatch.setenv('BACKUP_RCLONE_REMOTE', 'remote:school backups')
    calls = []
    monkeypatch.setattr(backup_sqlite.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    backup_sqlite.upload_backup(target)
    assert calls == [((['rclone', 'copy', str(target), 'remote:school backups'],), {'check': True, 'timeout': 300})]
