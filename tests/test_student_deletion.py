from contextlib import contextmanager
from datetime import date
import re
import sqlite3

import pytest
from starlette.requests import Request

from app import main
from app.database import connect
from app.emailer import create_reminder_logs, queue_email
from app.models import SCHEMA_SQL
from app.services import (
    add_course_package, delete_student, init_schema, list_today_lessons,
    record_lesson, request_renewal, seed_data, student_portal_data,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setenv('EMAIL_WORKER_ENABLED', '0')
    connection = connect(tmp_path / 'test.sqlite3')
    init_schema(connection, SCHEMA_SQL)
    seed_data(connection)
    # Shared email addresses must never be used to determine deletion scope.
    connection.execute("UPDATE students SET email = 'shared@example.com'")
    maria = connection.execute("SELECT id FROM students WHERE name = 'Maria'").fetchone()[0]
    teacher = connection.execute('SELECT id FROM teachers ORDER BY id').fetchone()[0]
    add_course_package(connection, maria, '小提琴', teacher, 3)
    for lesson in list_today_lessons(connection, {'role': 'admin'}, date(2026, 9, 7)):
        record_lesson(connection, {'id': 1, 'role': 'admin'}, lesson['id'], 'complete')
    for package in connection.execute('SELECT id, student_id FROM course_packages').fetchall():
        request_renewal(connection, package['student_id'], package['id'])
    create_reminder_logs(connection, 0)
    connection.execute('UPDATE course_packages SET active = 0 WHERE course_name = ?', ('小提琴',))
    connection.commit()

    @contextmanager
    def session():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    monkeypatch.setattr(main, 'db_session', session)
    yield connection
    connection.close()


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}


def admin(conn):
    return dict(conn.execute("SELECT * FROM users WHERE role='admin'").fetchone())


def student_id(conn):
    return conn.execute("SELECT id FROM students WHERE name='Maria'").fetchone()[0]


def request():
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})


def token(conn, sid=None, uid=None):
    return main.delete_confirmation.dumps({'student_id': sid or student_id(conn), 'user_id': uid or admin(conn)['id']})


def test_delete_cleans_all_relations_and_preserves_other_students(conn):
    sid = student_id(conn)
    before = snapshot(conn)
    packages = {r['id'] for r in conn.execute('SELECT id FROM course_packages WHERE student_id=?', (sid,))}
    schedules = {r['id'] for r in conn.execute('SELECT id, course_package_id FROM schedules') if r['course_package_id'] in packages}
    instances = {r['id'] for r in conn.execute('SELECT id, schedule_id FROM lesson_instances') if r['schedule_id'] in schedules}
    renewals = {r['id'] for r in conn.execute('SELECT id, course_package_id FROM renewal_requests') if r['course_package_id'] in packages}
    targets = {'course_package': packages, 'schedule': schedules, 'lesson_instance': instances, 'renewal_request': renewals}
    # An unrelated type with a colliding numeric ID must remain untouched.
    queue_email(conn, 'shared@example.com', 'student', 'unrelated', 'keep', 'other', sid)
    conn.commit()
    emails = [tuple(r) for r in conn.execute('SELECT * FROM email_logs ORDER BY id') if r['related_id'] not in targets.get(r['related_type'], set())]
    assert len(packages) == 2
    with conn:
        delete_student(conn, sid)
    after = snapshot(conn)
    assert after['students'] == [r for r in before['students'] if r[0] != sid]
    assert after['course_packages'] == [r for r in before['course_packages'] if r[0] not in packages]
    assert after['schedules'] == [r for r in before['schedules'] if r[0] not in schedules]
    assert after['lesson_instances'] == [r for r in before['lesson_instances'] if r[0] not in instances]
    assert after['lesson_records'] == [r for r in before['lesson_records'] if r[2] not in packages]
    assert after['lesson_transactions'] == [r for r in before['lesson_transactions'] if r[1] not in packages]
    assert after['balance_alerts'] == [r for r in before['balance_alerts'] if r[0] not in packages]
    assert after['renewal_requests'] == [r for r in before['renewal_requests'] if r[0] not in renewals]
    assert after['email_logs'] == emails
    for table in ('users', 'teachers', 'settings'):
        assert after[table] == before[table]
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert student_portal_data(conn, sid) is None


def test_delete_failure_restores_every_table(conn):
    before = snapshot(conn)
    conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON students BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError, match='blocked'):
        delete_student(conn, student_id(conn))
    # Even if a caller catches the exception and commits, nothing was partly deleted.
    conn.commit()
    assert snapshot(conn) == before
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_delete_participates_in_outer_rollback(conn):
    before = snapshot(conn)
    delete_student(conn, student_id(conn))
    conn.rollback()
    assert snapshot(conn) == before


def test_nonexistent_student_changes_nothing(conn):
    before = snapshot(conn)
    with pytest.raises(ValueError, match='学生不存在'):
        delete_student(conn, 99999)
    assert snapshot(conn) == before


def test_student_without_packages_can_be_deleted(conn):
    sid = conn.execute("INSERT INTO students(name,email) VALUES ('Empty','empty@example.com')").lastrowid
    with conn:
        delete_student(conn, sid)
    assert conn.execute('SELECT 1 FROM students WHERE id=?', (sid,)).fetchone() is None


def test_confirmation_is_read_only_and_post_deletes(conn):
    sid = student_id(conn)
    before = snapshot(conn)
    detail = main.student_detail(sid, request(), user=admin(conn)).body.decode()
    assert f'href="/students/{sid}/delete"' in detail
    page = main.confirm_student_delete(sid, request(), user=admin(conn)).body.decode()
    assert '确认删除 Maria' in page and '无法撤销' in page
    assert f'href="/students/{sid}">取消' in page
    assert snapshot(conn) == before
    confirmation = re.search(r'name="confirmation" value="([^"]+)"', page)[1]
    response = main.delete_student_route(sid, confirmation, user=admin(conn))
    assert response.status_code == 303 and response.headers['location'] == '/students'
    with pytest.raises(main.HTTPException) as exc:
        main.delete_student_route(sid, confirmation, user=admin(conn))
    assert exc.value.status_code == 404


def test_teacher_cannot_view_confirmation_or_delete(conn):
    teacher = dict(conn.execute("SELECT * FROM users WHERE username='wang'").fetchone())
    sid = student_id(conn)
    before = snapshot(conn)
    assert '删除学生' not in main.student_detail(sid, request(), user=teacher).body.decode()
    for operation in (lambda: main.confirm_student_delete(sid, request(), user=teacher),
                      lambda: main.delete_student_route(sid, token(conn), user=teacher)):
        with pytest.raises(main.HTTPException) as exc:
            operation()
        assert exc.value.status_code == 403
    assert snapshot(conn) == before


@pytest.mark.parametrize('kind', ['missing', 'tampered', 'other_student', 'other_admin', 'expired'])
def test_invalid_confirmation_rejected(conn, monkeypatch, kind):
    before = snapshot(conn)
    confirmation = token(conn)
    if kind == 'missing':
        confirmation = ''
    elif kind == 'tampered':
        confirmation += 'x'
    elif kind == 'other_student':
        confirmation = token(conn, sid=99999)
    elif kind == 'other_admin':
        confirmation = token(conn, uid=99999)
    elif kind == 'expired':
        import itsdangerous.timed
        now = itsdangerous.timed.time.time()
        monkeypatch.setattr(itsdangerous.timed.time, 'time', lambda: now + 901)
    with pytest.raises(main.HTTPException) as exc:
        main.delete_student_route(student_id(conn), confirmation, user=admin(conn))
    assert exc.value.status_code == 403
    assert snapshot(conn) == before


def test_confirmation_missing_student_is_404(conn):
    with pytest.raises(main.HTTPException) as exc:
        main.confirm_student_delete(99999, request(), user=admin(conn))
    assert exc.value.status_code == 404


def test_delete_routes_require_login(conn):
    routes = [r for r in main.app.routes if getattr(r, 'path', '') == '/students/{student_id}/delete']
    assert {method for r in routes for method in r.methods} == {'GET', 'POST'}
    assert all(any(d.call is main.get_user for d in r.dependant.dependencies) for r in routes)
    with pytest.raises(main.HTTPException) as exc:
        main.get_user(request())
    assert exc.value.status_code == 303
    assert exc.value.headers['Location'] == '/login'
