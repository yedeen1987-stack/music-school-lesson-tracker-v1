"""SMTP may block, but independent website writes must remain available."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from app.emailer import MAX_ATTEMPTS, process_email_queue, queue_email
from app.models import SCHEMA_SQL
from app.services import init_schema, seed_data, set_student_active


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'concurrent-mail.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        init_schema(conn, SCHEMA_SQL)
        seed_data(conn)
    monkeypatch.setattr('app.emailer.smtp_config', lambda: {'fake': True})
    return path


@pytest.mark.parametrize('previous', ['none', 'sent', 'retry', 'failed', 'suppressed'])
def test_slow_sender_does_not_block_another_connection(database, previous):
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        if previous != 'none':
            related = 'course_package' if previous == 'suppressed' else 'other'
            queue_email(conn, 'maria@example.com', 'student', 'previous', 'body', related, 1)
            if previous == 'failed':
                conn.execute('UPDATE email_logs SET attempts=?', (MAX_ATTEMPTS - 1,))
            if previous == 'suppressed':
                # Exercise send-time eligibility, without pre-populating suppression rows.
                conn.execute('UPDATE students SET active=0 WHERE id=1')
        queue_email(conn, 'other@example.com', 'student', 'slow', 'body', 'other', 2)
    entered = threading.Event()
    release = threading.Event()

    def worker():
        conn = sqlite3.connect(database, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            # The queue runner may arrive with pending initialization writes.
            conn.execute("INSERT INTO settings(key,value) VALUES ('worker-init','ready')")

            def sender(config, recipient, subject, body):
                if subject == 'previous':
                    if previous in ('retry', 'failed'):
                        raise RuntimeError('simulated SMTP failure')
                    return
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release sender')

            result = process_email_queue(conn, sender=sender)
            conn.commit()
            return result
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(5), 'worker did not enter slow SMTP sender'
            # Both INSERT and COMMIT must finish while SMTP is still blocked.
            with sqlite3.connect(database, timeout=0.2) as web:
                web.execute('PRAGMA foreign_keys=ON')
                web.execute("INSERT INTO lesson_instances(schedule_id,lesson_date) VALUES (1,'2026-01-01')")
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result == (2 if previous == 'sent' else 1, 1 if previous == 'failed' else 0)
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lesson_instances').fetchone()[0] == 1
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('archive_method', ['service', 'active_field'])
def test_archive_during_smtp_prevents_later_email_in_same_batch(database, archive_method):
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        for subject in ('in flight', 'still queued'):
            queue_email(conn, 'maria@example.com', 'student', subject, 'body', 'course_package', 1)
        queued_before = tuple(conn.execute("SELECT * FROM email_logs WHERE subject='still queued'").fetchone())
    entered = threading.Event()
    release = threading.Event()
    delivered = []

    def worker():
        conn = sqlite3.connect(database, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            def sender(config, recipient, subject, body):
                delivered.append(subject)
                if subject == 'in flight':
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test did not release sender')
            return process_email_queue(conn, sender=sender)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(5)
            with sqlite3.connect(database, timeout=0.2) as web:
                web.row_factory = sqlite3.Row
                if archive_method == 'service':
                    set_student_active(web, {'role': 'admin'}, 1, False)
                else:
                    web.execute('UPDATE students SET active=0 WHERE id=1')
        finally:
            release.set()
        assert future.result(timeout=5) == (1, 0)
    assert delivered == ['in flight']  # The single in-flight message is the accepted race.
    with sqlite3.connect(database) as conn:
        queued = conn.execute("SELECT * FROM email_logs WHERE subject='still queued'").fetchone()
        assert queued == queued_before
        assert conn.execute('SELECT 1 FROM email_suppressions WHERE email_log_id=?', (queued[0],)).fetchone()
