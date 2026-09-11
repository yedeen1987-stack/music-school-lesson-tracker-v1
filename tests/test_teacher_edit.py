import pytest
from starlette.requests import Request

from test_archive import conn, http, user, package, history, rows
from app import main
from app.emailer import notify_schedule_changed, process_email_queue
from app.services import edit_teacher_profile, set_teacher_active


def test_teacher_profile_edit_preserves_accounts_packages_and_history(conn):
    p = package(conn)
    before = history(conn)
    assert '编辑老师资料' in http(conn, '/teachers')[1]
    assert http(conn, f'/teachers/{p["teacher_id"]}/edit', 'POST', {'name':'王老师新姓名', 'email':'newteacher@example.com'})[0] == 303
    t = conn.execute('SELECT * FROM teachers WHERE id=?', (p['teacher_id'],)).fetchone()
    assert (t['name'], t['email']) == ('王老师新姓名', 'newteacher@example.com')
    assert history(conn) == before


@pytest.mark.parametrize('name,email', [('', 'x@example.com'), (' ', 'x@example.com'), ('X',''), ('X','bad'), ('X','a@b'), ('X','a b@example.com')])
def test_invalid_teacher_profile_redirects_without_writes(conn, name, email):
    p = package(conn)
    before = rows(conn, 'teachers')
    assert http(conn, f'/teachers/{p["teacher_id"]}/edit', 'POST', {'name':name,'email':email})[0] == 303
    assert rows(conn, 'teachers') == before
    page = main.teachers(Request({'type':'http','headers':[]}), user=user(conn), error='请输入有效的邮箱地址').body.decode()
    assert 'role="alert"' in page and '请输入有效的邮箱地址' in page


def test_only_admin_can_edit_teacher(conn):
    p = package(conn)
    before = rows(conn, 'teachers')
    assert http(conn, f'/teachers/{p["teacher_id"]}/edit', 'POST', {'name':'X','email':'x@example.com'}, 'wang')[0] == 403
    with pytest.raises(PermissionError):
        edit_teacher_profile(conn, user(conn, 'wang'), p['teacher_id'], 'X', 'x@example.com')
    assert rows(conn, 'teachers') == before


def test_new_teacher_email_receives_new_notices_and_old_pending_is_suppressed(conn, monkeypatch):
    p = package(conn)
    sid = conn.execute('SELECT id FROM schedules WHERE course_package_id=?', (p['id'],)).fetchone()[0]
    notify_schedule_changed(conn, sid)
    old = tuple(conn.execute("SELECT * FROM email_logs WHERE recipient_type='teacher'").fetchone())
    edit_teacher_profile(conn, user(conn), p['teacher_id'], '王老师', 'newteacher@example.com')
    notify_schedule_changed(conn, sid)
    monkeypatch.setattr('app.emailer.smtp_config', lambda: {'fake':True})
    calls = []
    process_email_queue(conn, sender=lambda *args: calls.append(args))
    assert 'newteacher@example.com' in [c[1] for c in calls]
    assert 'wang@example.com' not in [c[1] for c in calls]
    assert tuple(conn.execute('SELECT * FROM email_logs WHERE id=?', (old[0],)).fetchone()) == old


def test_archived_or_missing_teacher_cannot_be_edited(conn):
    p = package(conn)
    set_teacher_active(conn, user(conn), p['teacher_id'], False)
    before = rows(conn, 'teachers')
    for tid in (p['teacher_id'], 99999):
        with pytest.raises(ValueError):
            edit_teacher_profile(conn, user(conn), tid, 'X', 'x@example.com')
    assert rows(conn, 'teachers') == before
