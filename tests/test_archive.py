import asyncio
from contextlib import contextmanager
from datetime import date
import re
import sqlite3
from urllib.parse import urlencode

import pytest

from app import main
from app.database import connect
from app.emailer import (
    create_reminder_logs, notify_lesson_completed, notify_low_balance_if_needed,
    notify_schedule_changed, process_email_queue, email_queue_summary,
)
from app.models import SCHEMA_SQL
from app.security import sign_session, sign_student_token
from app.services import (
    add_course_package, assign_teacher, authenticate, current_user, ensure_today_instances,
    init_schema, list_today_lessons, list_unassigned_courses, record_lesson, request_renewal,
    seed_data, set_student_active, set_teacher_active, student_portal_data, undo_last_record,
    update_schedule,
)

DAY = date(2026, 9, 7)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'archive.sqlite3', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    init_schema(db, SCHEMA_SQL)
    seed_data(db)
    db.commit()
    monkeypatch.setenv('EMAIL_WORKER_ENABLED', '0')

    @contextmanager
    def session():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(main, 'db_session', session)
    yield db
    db.close()


def user(conn, name='admin'):
    return dict(conn.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone())


def package(conn, name='Maria'):
    return conn.execute('SELECT cp.* FROM course_packages cp JOIN students st ON st.id=cp.student_id WHERE st.name=? ORDER BY cp.id', (name,)).fetchone()


def rows(conn, table):
    return [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def history(conn):
    return {t: rows(conn, t) for t in ('course_packages', 'schedules', 'lesson_instances', 'lesson_records',
        'lesson_transactions', 'email_logs', 'balance_alerts', 'renewal_requests', 'users', 'settings')}


def http(conn, path, method='GET', data=None, username='admin'):
    """Exercise the real ASGI routes, forms, cookies and dependencies without a new HTTP dependency."""
    conn.commit()  # Fixture changes precede the independent HTTP request transaction.
    body = urlencode(data or {}).encode()
    headers = [(b'content-type', b'application/x-www-form-urlencoded')]
    if username:
        headers.append((b'cookie', ('session=' + sign_session(user(conn, username)['id'])).encode()))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
        'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
        'headers': headers, 'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    messages = []

    async def run():
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        await main.app(scope, receive, send)

    asyncio.run(run())
    status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
    content = b''.join(m.get('body', b'') for m in messages).decode()
    return status, content


def confirm(conn, kind, sid, action='deactivate', username='admin'):
    path = f'/archive/{kind}/{sid}/{action}'
    status, page = http(conn, path, username=username)
    assert status == 200
    token = re.search(r'name="confirmation" value="([^"]+)"', page)[1]
    return http(conn, path, 'POST', {'confirmation': token}, username=username)


def test_old_database_migration_preserves_all_rows(tmp_path):
    conn = connect(tmp_path / 'old.sqlite3')
    old = SCHEMA_SQL.replace('    phone TEXT,\n    email TEXT NOT NULL,\n    active INTEGER NOT NULL DEFAULT 1,', '    phone TEXT,\n    email TEXT NOT NULL,')
    conn.executescript(old)
    conn.execute("INSERT INTO students(name,email) VALUES ('Existing','same@example.com')")
    conn.commit()
    for _ in range(2):
        init_schema(conn, SCHEMA_SQL)
    assert dict(conn.execute('SELECT * FROM students').fetchone())['active'] == 1
    assert conn.execute('SELECT name FROM students').fetchone()[0] == 'Existing'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


def test_student_archive_preserves_history_and_balance_and_hides_existing_lessons(conn):
    p = package(conn)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    request_renewal(conn, p['student_id'], p['id'])
    before = history(conn)
    set_student_active(conn, user(conn), p['student_id'], False)
    assert history(conn) == before
    assert [r['student_name'] for r in list_today_lessons(conn, user(conn), DAY)] == ['Lucas']
    assert 'Maria' not in http(conn, '/students')[1]
    assert 'Maria' not in http(conn, '/schedule')[1]
    ensure_today_instances(conn, date(2026, 9, 14))
    assert conn.execute('SELECT COUNT(*) FROM lesson_instances WHERE lesson_date=?', ('2026-09-14',)).fetchone()[0] == 1
    set_student_active(conn, user(conn), p['student_id'], True)
    assert package(conn)['current_balance'] == p['current_balance'] - 1
    assert {r['student_name'] for r in list_today_lessons(conn, user(conn), DAY)} == {'Maria', 'Lucas'}
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_no_new_notifications_or_old_queued_delivery_after_archive(conn, monkeypatch):
    p = package(conn)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    schedule = conn.execute('SELECT id FROM schedules WHERE course_package_id=?', (p['id'],)).fetchone()[0]
    notify_schedule_changed(conn, schedule)
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    request_renewal(conn, p['student_id'], p['id'])
    set_student_active(conn, user(conn), p['student_id'], False)
    before = rows(conn, 'email_logs')
    notify_schedule_changed(conn, schedule)
    notify_lesson_completed(conn, lesson['id'], 1)
    notify_low_balance_if_needed(conn, p['id'], 0)
    assert rows(conn, 'email_logs') == before
    monkeypatch.setattr('app.emailer.smtp_config', lambda: {'fake': True})
    sent = []
    assert process_email_queue(conn, sender=lambda *args: sent.append(args)) == (0, 0)
    assert not sent and rows(conn, 'email_logs') == before
    assert email_queue_summary(conn)['pending'] == 0
    set_student_active(conn, user(conn), p['student_id'], True)
    assert process_email_queue(conn, sender=lambda *args: sent.append(args)) == (0, 0)
    assert not sent  # Restoration never replays the suppressed old notices.


def test_other_student_notices_still_work_and_shared_emails_are_not_suppressed(conn, monkeypatch):
    conn.execute("UPDATE students SET email='family@example.com'")
    p = package(conn)
    set_student_active(conn, user(conn), p['student_id'], False)
    create_reminder_logs(conn, 0)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    messages = conn.execute('SELECT * FROM email_logs').fetchall()
    assert messages and all('Lucas' in r['body'] and 'Maria' not in r['body'] for r in messages)
    monkeypatch.setattr('app.emailer.smtp_config', lambda: {'fake': True})
    assert process_email_queue(conn, sender=lambda *args: None)[0] == len(messages)


@pytest.mark.parametrize('action', ['complete', 'skip', 'undo'])
def test_stale_lesson_actions_cannot_change_archived_balance(conn, action):
    p = package(conn)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    if action == 'undo':
        record_lesson(conn, user(conn), lesson['id'], 'complete')
    set_student_active(conn, user(conn), p['student_id'], False)
    before = history(conn)
    with pytest.raises(PermissionError):
        if action == 'undo':
            undo_last_record(conn, user(conn), conn.execute('SELECT MAX(id) FROM lesson_records').fetchone()[0])
        else:
            record_lesson(conn, user(conn), lesson['id'], action)
    assert history(conn) == before


def test_portal_get_and_renewal_post_are_disabled(conn):
    p = package(conn)
    link = '/p/' + sign_student_token(p['student_id'])
    set_student_active(conn, user(conn), p['student_id'], False)
    assert student_portal_data(conn, p['student_id']) is None
    assert http(conn, link, username=None)[0] == 404
    assert http(conn, link + '/renew/' + str(p['id']), 'POST', username=None)[0] == 404
    set_student_active(conn, user(conn), p['student_id'], True)
    assert http(conn, link, username=None)[0] == 200


def test_teacher_deactivation_unassigned_reassignment_and_archive_snapshot(conn):
    p = package(conn)
    old_teacher, new_teacher = p['teacher_id'], package(conn, 'Lucas')['teacher_id']
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    before = history(conn)
    set_teacher_active(conn, user(conn), old_teacher, False)
    assert history(conn) == before
    assert [r['id'] for r in list_unassigned_courses(conn)] == [p['id']]
    assert [r['student_name'] for r in list_today_lessons(conn, user(conn), DAY)] == ['Lucas']
    assert '1 门课程暂无老师' in http(conn, '/')[1]
    assert '原老师：王老师' in http(conn, '/unassigned')[1]
    for path in ('/students/new', '/schedule', '/teachers'):
        assert '王老师' not in http(conn, path)[1]
    assert authenticate(conn, 'wang', 'teacher123') is None
    assert current_user(conn, user(conn, 'wang')['id']) is None
    assert http(conn, '/students', username='wang')[0] == 303
    assign_teacher(conn, user(conn), p['id'], new_teacher)
    assert list_unassigned_courses(conn) == []
    assert 'Maria' in [r['student_name'] for r in list_today_lessons(conn, user(conn, 'li'), DAY)]
    archive = http(conn, f'/archive/teachers/{old_teacher}')[1]
    assert 'Maria' in archive and '停用时余额 9 节' in archive and 'complete' in archive
    set_teacher_active(conn, user(conn), old_teacher, True)
    assert package(conn)['teacher_id'] == new_teacher
    assert authenticate(conn, 'wang', 'teacher123') is not None


def test_teacher_no_mail_and_unassigned_notices_do_not_starve_other_queue(conn, monkeypatch):
    create_reminder_logs(conn, 0)
    p = package(conn)
    set_teacher_active(conn, user(conn), p['teacher_id'], False)
    before = [tuple(r) for r in conn.execute("SELECT * FROM email_logs WHERE body LIKE '%Maria%'")]
    create_reminder_logs(conn, 0)
    monkeypatch.setattr('app.emailer.smtp_config', lambda: {'fake': True})
    calls = []
    assert process_email_queue(conn, limit=1, sender=lambda *args: calls.append(args)) == (1, 0)
    assert 'Lucas' in calls[0][-1]
    assert [tuple(r) for r in conn.execute("SELECT * FROM email_logs WHERE body LIKE '%Maria%'")] == before


def test_restored_student_with_inactive_teacher_is_unassigned(conn):
    p = package(conn)
    set_student_active(conn, user(conn), p['student_id'], False)
    set_teacher_active(conn, user(conn), p['teacher_id'], False)
    assert list_unassigned_courses(conn) == []
    set_student_active(conn, user(conn), p['student_id'], True)
    assert [r['id'] for r in list_unassigned_courses(conn)] == [p['id']]
    assert package(conn)['current_balance'] == p['current_balance']
    set_teacher_active(conn, user(conn), p['teacher_id'], True)
    assert list_unassigned_courses(conn) == []
    assert 'Maria' in [r['student_name'] for r in list_today_lessons(conn, user(conn), DAY)]


def test_permissions_own_teacher_can_archive_and_restore_but_not_others(conn):
    maria, lucas = package(conn), package(conn, 'Lucas')
    assert confirm(conn, 'students', maria['student_id'], username='wang')[0] == 303
    assert 'Maria' in http(conn, '/archive', username='wang')[1]
    assert 'Maria' not in http(conn, '/archive', username='li')[1]
    assert confirm(conn, 'students', maria['student_id'], 'restore', 'wang')[0] == 303
    for kind, sid in [('students', lucas['student_id']), ('teachers', maria['teacher_id'])]:
        assert http(conn, f'/archive/{kind}/{sid}/deactivate', username='wang')[0] == 403
        signed = main.archive_signer.dumps([user(conn, 'wang')['id'], kind, sid, 'deactivate'])
        assert http(conn, f'/archive/{kind}/{sid}/deactivate', 'POST', {'confirmation': signed}, 'wang')[0] == 403
    assert http(conn, '/unassigned', username='wang')[0] == 403
    assert http(conn, f'/unassigned/{maria["id"]}', 'POST', {'teacher_id': lucas['teacher_id']}, 'wang')[0] == 403


def test_confirmation_and_archive_history_page(conn):
    p = package(conn)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    before = history(conn)
    status, page = http(conn, f'/archive/students/{p["student_id"]}/deactivate')
    assert status == 200 and '确定移除 Maria' in page and '剩余 9 节' in page and '随时可以恢复' in page
    assert history(conn) == before
    assert conn.execute('SELECT active FROM students WHERE id=?', (p['student_id'],)).fetchone()[0] == 1
    assert confirm(conn, 'students', p['student_id'])[0] == 303
    detail = http(conn, '/students/' + str(p['student_id']))[1]
    assert '已停用' in detail and 'complete' in detail and '余额 9' in detail and '恢复学生' in detail
    assert '续费课时' not in detail


@pytest.mark.parametrize('case', ['missing', 'tampered', 'wrong_student', 'wrong_user', 'wrong_action', 'expired'])
def test_invalid_confirmation_never_updates_state(conn, monkeypatch, case):
    p = package(conn)
    payload = [user(conn)['id'], 'students', p['student_id'], 'deactivate']
    if case == 'wrong_student':
        payload[2] = 9999
    elif case == 'wrong_user':
        payload[0] = 9999
    elif case == 'wrong_action':
        payload[3] = 'restore'
    token = main.archive_signer.dumps(payload)
    if case == 'tampered':
        token += 'x'
    if case == 'expired':
        import itsdangerous.timed
        now = itsdangerous.timed.time.time()
        monkeypatch.setattr(itsdangerous.timed.time, 'time', lambda: now + 901)
    data = {} if case == 'missing' else {'confirmation': token}
    assert http(conn, f'/archive/students/{p["student_id"]}/deactivate', 'POST', data)[0] in (403, 422)
    assert conn.execute('SELECT active FROM students WHERE id=?', (p['student_id'],)).fetchone()[0] == 1


def test_unauthenticated_archive_routes_require_login(conn):
    for path in ('/archive', '/unassigned', '/archive/students/1/deactivate'):
        assert http(conn, path, username=None)[0] == 303
    assert http(conn, '/archive/students/1/deactivate', 'POST', {'confirmation': 'x'}, username=None)[0] == 303


def test_post_rejects_inactive_teachers_and_archived_student_changes(conn):
    p = package(conn)
    set_teacher_active(conn, user(conn), p['teacher_id'], False)
    with pytest.raises(ValueError):
        add_course_package(conn, p['student_id'], 'test', p['teacher_id'], 1)
    assert http(conn, f'/unassigned/{p["id"]}', 'POST', {'teacher_id': p['teacher_id']})[0] == 303
    assert package(conn)['teacher_id'] == p['teacher_id']
    sid = conn.execute('SELECT id FROM schedules WHERE course_package_id=?', (p['id'],)).fetchone()[0]
    with pytest.raises(ValueError):
        update_schedule(conn, sid, p['teacher_id'], 0, '12:00')
    set_student_active(conn, user(conn), p['student_id'], False)
    with pytest.raises(ValueError):
        add_course_package(conn, p['student_id'], 'test', package(conn, 'Lucas')['teacher_id'], 1)


def test_archive_transaction_rolls_back_on_mail_suppression_failure(conn, monkeypatch):
    p = package(conn)
    before = history(conn)
    def fail(_):
        raise RuntimeError('suppression failed')
    monkeypatch.setattr('app.services.suppress_inactive_emails', fail)
    with pytest.raises(RuntimeError), conn:
        set_student_active(conn, user(conn), p['student_id'], False)
    assert conn.execute('SELECT active FROM students WHERE id=?', (p['student_id'],)).fetchone()[0] == 1
    assert history(conn) == before


def test_multicourse_balances_and_individual_active_flags_survive_restore(conn):
    p = package(conn)
    other = add_course_package(conn, p['student_id'], 'Other', p['teacher_id'], 4)
    conn.execute('UPDATE course_packages SET active=0 WHERE id=?', (other,))
    conn.execute('UPDATE schedules SET active=0 WHERE course_package_id=?', (p['id'],))
    before = history(conn)
    set_student_active(conn, user(conn), p['student_id'], False)
    set_student_active(conn, user(conn), p['student_id'], True)
    assert history(conn) == before
    assert list_today_lessons(conn, user(conn), DAY)[0]['student_name'] == 'Lucas'


def test_teacher_confirmation_preserves_records_and_restores(conn):
    p = package(conn)
    before = history(conn)
    status, page = http(conn, f'/archive/teachers/{p["teacher_id"]}/deactivate')
    assert status == 200 and '确定移除 王老师' in page and '无老师' in page
    assert history(conn) == before
    assert confirm(conn, 'teachers', p['teacher_id'])[0] == 303
    assert '王老师' in http(conn, '/archive')[1]
    assert confirm(conn, 'teachers', p['teacher_id'], 'restore')[0] == 303
    assert conn.execute('SELECT active FROM teachers WHERE id=?', (p['teacher_id'],)).fetchone()[0] == 1


def test_student_without_packages_has_archive_details(conn):
    sid = conn.execute("INSERT INTO students(name,email) VALUES ('Empty','empty@example.com')").lastrowid
    assert confirm(conn, 'students', sid)[0] == 303
    assert http(conn, f'/students/{sid}')[0] == 200
    assert 'Empty' in http(conn, '/archive')[1]


def test_archived_record_undo_and_renewal_handling_are_read_only(conn):
    p = package(conn)
    lesson = list_today_lessons(conn, user(conn), DAY)[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    rid = conn.execute('SELECT MAX(id) FROM lesson_records').fetchone()[0]
    renewal = request_renewal(conn, p['student_id'], p['id'])
    set_student_active(conn, user(conn), p['student_id'], False)
    before = history(conn)
    assert f'action="/records/{rid}/undo"' not in http(conn, '/records')[1]
    assert http(conn, f'/records/{rid}/undo', 'POST')[0] == 403
    assert http(conn, f'/renewals/{renewal}/handled', 'POST')[0] == 404
    assert history(conn) == before


def test_assign_post_and_stale_assignment_preserve_balance(conn):
    p = package(conn)
    next_teacher = package(conn, 'Lucas')['teacher_id']
    set_teacher_active(conn, user(conn), p['teacher_id'], False)
    assert http(conn, f'/unassigned/{p["id"]}', 'POST', {'teacher_id': next_teacher})[0] == 303
    assert package(conn)['teacher_id'] == next_teacher
    assert package(conn)['current_balance'] == p['current_balance']
    with pytest.raises(ValueError):
        assign_teacher(conn, user(conn), p['id'], next_teacher)
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_nonexistent_archive_subject_returns_404(conn):
    assert http(conn, '/archive/students/9999/deactivate')[0] == 404
    assert http(conn, '/archive/teachers/9999/restore')[0] == 404


def test_teacher_archive_suppression_failure_rolls_back_snapshot(conn, monkeypatch):
    p = package(conn)
    def fail(_):
        raise RuntimeError('suppression failed')
    monkeypatch.setattr('app.services.suppress_inactive_emails', fail)
    with pytest.raises(RuntimeError), conn:
        set_teacher_active(conn, user(conn), p['teacher_id'], False)
    assert conn.execute('SELECT active FROM teachers WHERE id=?', (p['teacher_id'],)).fetchone()[0] == 1
    assert rows(conn, 'teacher_archive_courses') == []
