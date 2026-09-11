from datetime import date
import sqlite3

import pytest

from test_archive import conn, http, user, package, history
from app.database import connect
from app.models import SCHEMA_SQL
from app.services import (
    adjust_package_balance, edit_student_profile, init_schema, list_today_lessons,
    record_lesson, rename_course_package, seed_data, set_student_active,
)


def test_profile_edit_changes_next_notification_address(conn):
    p = package(conn)
    assert http(conn, f'/students/{p["student_id"]}/edit', 'POST',
                {'name': 'Maria Updated', 'phone': '123', 'email': 'new@example.com'})[0] == 303
    student = conn.execute('SELECT * FROM students WHERE id=?', (p['student_id'],)).fetchone()
    assert (student['name'], student['phone'], student['email']) == ('Maria Updated', '123', 'new@example.com')
    lesson = list_today_lessons(conn, user(conn), date(2026, 9, 7))[0]
    record_lesson(conn, user(conn), lesson['id'], 'complete')
    assert {r[0] for r in conn.execute("SELECT recipient_email FROM email_logs WHERE recipient_type='student'")} == {'new@example.com'}


@pytest.mark.parametrize('email', ['', 'bad', 'a@', '@example.com', 'a@b', 'a b@example.com', 'a@@example.com'])
def test_invalid_email_redirects_without_modifying_student(conn, email):
    p = package(conn)
    before = tuple(conn.execute('SELECT * FROM students WHERE id=?', (p['student_id'],)).fetchone())
    assert http(conn, f'/students/{p["student_id"]}/edit', 'POST', {'name': 'Changed', 'email': email})[0] == 303
    assert tuple(conn.execute('SELECT * FROM students WHERE id=?', (p['student_id'],)).fetchone()) == before
    from app import main
    from starlette.requests import Request
    page = main.student_detail(p['student_id'], Request({'type': 'http', 'headers': []}), user=user(conn), error='请输入有效的邮箱地址').body.decode()
    assert 'role="alert"' in page and '请输入有效的邮箱地址' in page


def test_course_rename_keeps_balance_and_only_changes_selected_package(conn):
    p = package(conn)
    other = tuple(package(conn, 'Lucas'))
    response = http(conn, f'/students/{p["student_id"]}/packages/{p["id"]}/rename', 'POST', {'course_name': '乐理'})
    assert response[0] == 303
    changed = conn.execute('SELECT * FROM course_packages WHERE id=?', (p['id'],)).fetchone()
    assert changed['course_name'] == '乐理' and changed['current_balance'] == p['current_balance']
    assert tuple(package(conn, 'Lucas')) == other


def test_adjust_records_actor_reason_before_after_and_matching_transaction(conn):
    p = package(conn)
    before_instances = conn.execute('SELECT COUNT(*) FROM lesson_instances').fetchone()[0]
    assert http(conn, f'/students/{p["student_id"]}/packages/{p["id"]}/adjust', 'POST',
                {'balance': '3', 'reason': '录入错误，实际剩余 3 节'})[0] == 303
    record = conn.execute("SELECT * FROM lesson_records WHERE action='adjust'").fetchone()
    transaction = conn.execute('SELECT * FROM lesson_transactions WHERE lesson_record_id=?', (record['id'],)).fetchone()
    assert record['lesson_instance_id'] is None
    assert (record['delta_lessons'], record['balance_after'], record['actor_user_id']) == (-7, 3, user(conn)['id'])
    assert transaction['delta_lessons'] == -7 and transaction['reason'] == record['note']
    assert conn.execute('SELECT SUM(delta_lessons) FROM lesson_transactions WHERE course_package_id=?', (p['id'],)).fetchone()[0] == package(conn)['current_balance'] == 3
    assert conn.execute('SELECT COUNT(*) FROM lesson_instances').fetchone()[0] == before_instances
    for path in ('/records', f'/students/{p["student_id"]}'):
        body = http(conn, path)[1]
        assert '调整课时' in body and '从 10 节调到 3 节' in body and 'admin' in body and '录入错误，实际剩余 3 节' in body
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('balance,reason', [('3', ''), ('3', '  '), ('-1', '错误'), ('3.5', '错误'), ('', '错误'), ('abc', '错误')])
def test_invalid_adjustment_changes_nothing(conn, balance, reason):
    p = package(conn)
    before = history(conn)
    assert http(conn, f'/students/{p["student_id"]}/packages/{p["id"]}/adjust', 'POST',
                {'balance': balance, 'reason': reason})[0] == 303
    assert history(conn) == before


def test_adjust_to_zero_is_allowed_and_sends_low_balance_notice(conn):
    p = package(conn)
    with conn:
        adjust_package_balance(conn, user(conn), p['id'], 0, '退课')
    assert package(conn)['current_balance'] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_logs WHERE subject='课时余额提醒'").fetchone()[0] == 2


def test_raise_balance_rearms_low_balance_alerts(conn):
    p = package(conn)
    conn.execute("UPDATE settings SET value='3' WHERE key='low_balance_thresholds'")
    with conn:
        adjust_package_balance(conn, user(conn), p['id'], 3, '纠正录入')
    first = conn.execute("SELECT COUNT(*) FROM email_logs WHERE subject='课时余额提醒'").fetchone()[0]
    assert first == 2
    with conn:
        adjust_package_balance(conn, user(conn), p['id'], 20, '恢复正确余额')
        adjust_package_balance(conn, user(conn), p['id'], 3, '扣回错误增加')
    assert conn.execute("SELECT COUNT(*) FROM email_logs WHERE subject='课时余额提醒'").fetchone()[0] == first + 2


@pytest.mark.parametrize('operation,form', [('edit', {'name':'X','email':'x@example.com'}),
    ('rename', {'course_name':'X'}), ('adjust', {'balance':'0','reason':'X'})])
def test_teacher_cannot_edit_or_adjust(conn, operation, form):
    p = package(conn)
    path = f'/students/{p["student_id"]}/edit' if operation == 'edit' else f'/students/{p["student_id"]}/packages/{p["id"]}/{operation}'
    before = history(conn)
    assert http(conn, path, 'POST', form, 'wang')[0] == 403
    assert history(conn) == before
    with pytest.raises(PermissionError):
        adjust_package_balance(conn, user(conn, 'wang'), p['id'], 0, 'X')


def test_mismatched_student_package_cannot_change_another_student(conn):
    p, other = package(conn), package(conn, 'Lucas')
    before = history(conn)
    assert http(conn, f'/students/{p["student_id"]}/packages/{other["id"]}/adjust', 'POST', {'balance':'0','reason':'X'})[0] == 404
    assert history(conn) == before


def test_adjustment_rolls_back_balance_record_and_emails_if_transaction_fails(conn):
    p = package(conn)
    conn.execute("CREATE TRIGGER fail_transaction BEFORE INSERT ON lesson_transactions BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    conn.commit()
    before = history(conn)
    with pytest.raises(sqlite3.IntegrityError, match='blocked'):
        adjust_package_balance(conn, user(conn), p['id'], 3, 'test')
    conn.commit()
    assert history(conn) == before


def test_archived_students_cannot_be_edited_or_adjusted(conn):
    p = package(conn)
    set_student_active(conn, user(conn), p['student_id'], False)
    conn.commit()
    with pytest.raises(ValueError):
        adjust_package_balance(conn, user(conn), p['id'], 1, 'test')
    with pytest.raises(ValueError):
        edit_student_profile(conn, user(conn), p['student_id'], 'X', '', 'x@example.com')
    with pytest.raises(ValueError):
        rename_course_package(conn, user(conn), p['id'], 'X')


def test_legacy_record_migration_preserves_history_indexes_and_foreign_keys(tmp_path):
    db = connect(tmp_path / 'legacy.sqlite3')
    old_schema = SCHEMA_SQL.replace('lesson_instance_id INTEGER,', 'lesson_instance_id INTEGER NOT NULL,', 1).replace("    CHECK (action = 'adjust' OR lesson_instance_id IS NOT NULL),\n", '', 1)
    db.executescript(old_schema)
    seed_data(db)
    lesson = list_today_lessons(db, user(db), date(2026, 9, 7))[0]
    record_lesson(db, user(db), lesson['id'], 'complete')
    db.execute('CREATE INDEX legacy_record_index ON lesson_records(course_package_id)')
    db.commit()
    before = history(db)
    for _ in range(2):
        init_schema(db, SCHEMA_SQL)
    assert history(db) == before
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_record_index'").fetchone()
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with db:
        adjust_package_balance(db, user(db), package(db)['id'], 4, '迁移后调整')
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert package(db)['current_balance'] == 4
    db.close()


def test_non_adjustment_record_still_requires_real_lesson(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO lesson_records(course_package_id,action,delta_lessons,balance_after) VALUES (1,'complete',-1,9)")


def test_legacy_migration_failure_restores_table_and_foreign_keys(tmp_path):
    class FailingConnection(sqlite3.Connection):
        fail_rename = False
        def execute(self, sql, *args, **kwargs):
            if self.fail_rename and sql == 'ALTER TABLE lesson_records_new RENAME TO lesson_records':
                raise sqlite3.OperationalError('simulated migration failure')
            return super().execute(sql, *args, **kwargs)
    db = sqlite3.connect(tmp_path / 'rollback.sqlite3', factory=FailingConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA_SQL.replace('lesson_instance_id INTEGER,', 'lesson_instance_id INTEGER NOT NULL,', 1).replace("    CHECK (action = 'adjust' OR lesson_instance_id IS NOT NULL),\n", '', 1))
    seed_data(db)
    lesson = list_today_lessons(db, user(db), date(2026, 9, 7))[0]
    record_lesson(db, user(db), lesson['id'], 'complete')
    db.commit()
    before = history(db)
    db.fail_rename = True
    with pytest.raises(sqlite3.OperationalError, match='simulated migration failure'):
        init_schema(db, SCHEMA_SQL)
    assert history(db) == before
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='lesson_records_new'").fetchone()
    db.close()
