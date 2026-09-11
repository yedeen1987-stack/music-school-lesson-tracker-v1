import os
import re
import threading
import time
from datetime import date

if os.getenv("TZ") and hasattr(time, "tzset"):
    time.tzset()

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.database import db_session
from app.emailer import process_email_queue, email_queue_summary
from app.models import SCHEMA_SQL
from app.security import SECRET_KEY, read_session, read_student_token, sign_session
from app.services import (
    add_course_package,
    adjust_package_balance,
    edit_student_profile,
    edit_teacher_profile,
    rename_course_package
    assign_teacher,
    list_unassigned_courses,
    require_student_access,
    set_student_active,
    set_teacher_active,
    authenticate,
    change_password,
    list_renewal_requests,
    mark_renewal_handled,
    overdraft_limit,
    request_renewal,
    student_portal_data,
    student_portal_url,
    create_student,
    current_user,
    ensure_default_settings,
    init_schema,
    list_today_lessons,
    record_lesson,
    undo_last_record,
    update_setting,
    update_schedule,
)

app = FastAPI(title="音乐机构课时记录 V1")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
archive_signer = URLSafeTimedSerializer(SECRET_KEY, salt="music-school-archive")


def cookie_secure_enabled():
    value = os.getenv("COOKIE_SECURE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


EMAIL_WORKER_INTERVAL = int(os.getenv("EMAIL_WORKER_INTERVAL", "20"))


def email_worker_loop():
    """后台线程按固定间隔发送邮件队列，页面请求不再等待 SMTP。"""
    while True:
        try:
            with db_session() as conn:
                process_email_queue(conn)
        except Exception:
            pass
        time.sleep(EMAIL_WORKER_INTERVAL)


@app.on_event("startup")
def startup():
    with db_session() as conn:
        init_schema(conn, SCHEMA_SQL)
        ensure_default_settings(conn)
    if os.getenv("EMAIL_WORKER_ENABLED", "0") == "1":
        threading.Thread(target=email_worker_loop, daemon=True).start()


def get_user(request: Request):
    with db_session() as conn:
        user = current_user(conn, read_session(request.cookies.get("session")))
        if not user:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return dict(user)


def render(request, name, context=None):
    context = context or {}
    return templates.TemplateResponse(name, {"request": request, **context})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    with db_session() as conn:
        user = authenticate(conn, username, password)
        if not user:
            return RedirectResponse("/login?error=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "session",
            sign_session(user["id"]),
            httponly=True,
            samesite="lax",
            secure=cookie_secure_enabled(),
        )
        return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        "session",
        httponly=True,
        samesite="lax",
        secure=cookie_secure_enabled(),
    )
    return response


@app.get("/", response_class=HTMLResponse)
def today(request: Request, error: str = "", user=Depends(get_user)):
    with db_session() as conn:
        lessons = list_today_lessons(conn, user)
        teachers = conn.execute("SELECT * FROM teachers WHERE active = 1 ORDER BY id").fetchall()
        pending_renewals = len(list_renewal_requests(conn, user))
        return render(
            request,
            "today.html",
            {
                "user": user,
                "lessons": lessons,
                "teachers": teachers,
                "today": date.today(),
                "overdraft": overdraft_limit(conn),
                "pending_renewals": pending_renewals,
                "unassigned_count": len(list_unassigned_courses(conn)) if user["role"] == "admin" else 0,
                "email_summary": email_queue_summary(conn),
                "error": error,
            },
        )


@app.post("/lessons/{lesson_id}/{action}")
def lesson_action(lesson_id: int, action: str, user=Depends(get_user)):
    with db_session() as conn:
        try:
            record_lesson(conn, user, lesson_id, action)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/records/{record_id}/undo")
def undo(record_id: int, user=Depends(get_user)):
    with db_session() as conn:
        try:
            undo_last_record(conn, user, record_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return RedirectResponse("/records", status_code=303)


@app.get("/students", response_class=HTMLResponse)
def students(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        where = "WHERE st.active=1 AND cp.active=1"
        params = []
        if user["role"] == "teacher":
            where += " AND cp.teacher_id = ?"
            params.append(user["teacher_id"])
        rows = conn.execute(
            f"""
            SELECT st.id, st.name, st.phone, st.email, cp.course_name, cp.current_balance, CASE WHEN t.active=1 THEN t.name ELSE '无老师' END AS teacher_name
            FROM students st
            JOIN course_packages cp ON cp.student_id = st.id
            JOIN teachers t ON t.id = cp.teacher_id
            {where}
            ORDER BY st.name
            """,
            params,
        ).fetchall()
        return render(request, "students.html", {"user": user, "students": rows})


@app.get("/students/new", response_class=HTMLResponse)
def new_student(request: Request, user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        teachers = conn.execute("SELECT * FROM teachers WHERE active = 1").fetchall()
        return render(request, "student_form.html", {"user": user, "teachers": teachers})


@app.post("/students")
def create_student_route(
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(...),
    course_name: str = Form(...),
    teacher_id: int = Form(...),
    purchased_lessons: int = Form(...),
    weekday: int = Form(...),
    planned_time: str = Form(...),
    user=Depends(get_user),
):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        try:
            create_student(conn, name, phone, email, course_name, teacher_id, purchased_lessons, weekday, planned_time)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return RedirectResponse("/students", status_code=303)


@app.get("/students/{student_id}", response_class=HTMLResponse)
def student_detail(student_id: int, request: Request, user=Depends(get_user), error: str = ""):
    with db_session() as conn:
        teacher_clause = "AND cp.teacher_id = ?" if user["role"] == "teacher" else ""
        params = [student_id] + ([user["teacher_id"]] if user["role"] == "teacher" else [])
        rows = conn.execute(
            f"""
            SELECT st.*, cp.id AS package_id, cp.course_name, cp.current_balance, cp.purchased_lessons, CASE WHEN t.active=1 THEN t.name ELSE '无老师（原老师：' || t.name || '）' END AS teacher_name
            FROM students st
            LEFT JOIN course_packages cp ON cp.student_id = st.id
            LEFT JOIN teachers t ON t.id = cp.teacher_id
            WHERE st.id = ? {teacher_clause}
            """,
            params,
        ).fetchall()
        if not rows:
            raise HTTPException(404)
        teachers = conn.execute("SELECT * FROM teachers WHERE active = 1").fetchall()
        return render(
            request,
            "student_detail.html",
            {
                "user": user,
                "student": rows[0],
                "error": error,
                "packages": [row for row in rows if row["package_id"] is not None],
                "teachers": teachers,
                "portal_url": student_portal_url(conn, student_id),
                "records": conn.execute(f"""SELECT lr.*, cp.course_name, u.username AS actor_name FROM lesson_records lr
                    JOIN course_packages cp ON cp.id=lr.course_package_id
                    LEFT JOIN users u ON u.id=lr.actor_user_id
                    WHERE cp.student_id=? {teacher_clause} ORDER BY lr.id DESC""", params).fetchall(),
            },
        )


@app.post("/students/{student_id}/packages")
def add_package(student_id: int, course_name: str = Form(...), teacher_id: int = Form(...), purchased_lessons: int = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        try:
            add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return RedirectResponse(f"/students/{student_id}", status_code=303)


@app.get("/teachers", response_class=HTMLResponse)
def teachers(request: Request, user=Depends(get_user), error: str = ""):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM teachers WHERE active=1 ORDER BY id").fetchall()
        return render(request, "teachers.html", {"user": user, "teachers": rows, "error": error})


@app.post("/teachers")
def add_teacher(name: str = Form(...), email: str = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        conn.execute("INSERT INTO teachers (name, email) VALUES (?, ?)", (name, email))
    return RedirectResponse("/teachers", status_code=303)


@app.get("/schedule", response_class=HTMLResponse)
def schedule(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        where = "WHERE st.active=1 AND cp.active=1 AND s.active=1 AND t.active=1"
        if user["role"] == "teacher":
            where += " AND cp.teacher_id = ?"
        params = [user["teacher_id"]] if user["role"] == "teacher" else []
        rows = conn.execute(
            f"""
            SELECT s.*, st.name AS student_name, cp.course_name, t.name AS teacher_name
            FROM schedules s
            JOIN course_packages cp ON cp.id = s.course_package_id
            JOIN students st ON st.id = cp.student_id
            JOIN teachers t ON t.id = cp.teacher_id
            {where}
            ORDER BY s.weekday, s.planned_time
            """,
            params,
        ).fetchall()
        teachers = conn.execute("SELECT * FROM teachers WHERE active = 1").fetchall()
        return render(request, "schedule.html", {"user": user, "schedules": rows, "teachers": teachers})


@app.post("/schedule/{schedule_id}")
def edit_schedule(schedule_id: int, teacher_id: int = Form(...), weekday: int = Form(...), planned_time: str = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        try:
            update_schedule(conn, schedule_id, teacher_id, weekday, planned_time)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return RedirectResponse("/schedule", status_code=303)


@app.get("/records", response_class=HTMLResponse)
def records(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        where = "WHERE cp.teacher_id = ?" if user["role"] == "teacher" else ""
        params = [user["teacher_id"]] if user["role"] == "teacher" else []
        rows = conn.execute(
            f"""
            SELECT lr.*, u.username AS actor_name, st.name AS student_name, cp.course_name, t.name AS teacher_name,
                   (st.active=1 AND cp.active=1 AND t.active=1) AS can_undo
            FROM lesson_records lr
            JOIN course_packages cp ON cp.id = lr.course_package_id
            JOIN students st ON st.id = cp.student_id
            JOIN teachers t ON t.id = cp.teacher_id
            LEFT JOIN users u ON u.id = lr.actor_user_id
            {where}
            ORDER BY lr.id DESC
            LIMIT 100
            """,
            params,
        ).fetchall()
        return render(request, "records.html", {"user": user, "records": rows})


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, error: str = "", user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        settings_rows = conn.execute("SELECT * FROM settings").fetchall()
        settings_map = {row["key"]: row["value"] for row in settings_rows}
        logs = conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT 50").fetchall()
        return render(request, "settings.html", {"user": user, "settings": settings_rows, "settings_map": settings_map, "logs": logs, "email_summary": email_queue_summary(conn), "error": error})


@app.post("/settings")
def save_settings(
    reminder_time: str = Form(...),
    low_balance_thresholds: str = Form(...),
    overdraft_limit_value: str = Form("0"),
    public_base_url: str = Form(""),
    user=Depends(get_user),
):
    if user["role"] != "admin":
        raise HTTPException(403)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time):
        # 和其他表单一致：跳回页面顶部显示提示，而不是抛出一个白底错误页
        return RedirectResponse(f"/settings?error={quote('提醒时间必须是 HH:MM（00:00–23:59）')}", status_code=303)
    with db_session() as conn:
        update_setting(conn, "reminder_time", reminder_time)
        update_setting(conn, "low_balance_thresholds", low_balance_thresholds)
        update_setting(conn, "overdraft_limit", overdraft_limit_value)
        update_setting(conn, "public_base_url", public_base_url.strip())
    return RedirectResponse("/settings", status_code=303)


@app.get("/password", response_class=HTMLResponse)
def password_page(request: Request, error: str = "", saved: int = 0, user=Depends(get_user)):
    return render(request, "password.html", {"user": user, "error": error, "saved": saved})


@app.post("/password")
def update_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(get_user),
):
    with db_session() as conn:
        try:
            change_password(conn, user["id"], current_password, new_password)
        except (PermissionError, ValueError) as exc:
            return RedirectResponse(f"/password?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/password?saved=1", status_code=303)


@app.get("/renewals", response_class=HTMLResponse)
def renewals(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        return render(
            request,
            "renewals.html",
            {
                "user": user,
                "open_requests": list_renewal_requests(conn, user, "open"),
                "handled_requests": list_renewal_requests(conn, user, "handled"),
            },
        )


@app.post("/renewals/{renewal_id}/handled")
def handle_renewal(renewal_id: int, user=Depends(get_user)):
    with db_session() as conn:
        try:
            mark_renewal_handled(conn, user, renewal_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
    return RedirectResponse("/renewals", status_code=303)


@app.get("/p/{token}", response_class=HTMLResponse)
def student_portal(token: str, request: Request, sent: int = 0):
    """学生用签名链接查看自己的课时，不需要账号密码。"""
    student_id = read_student_token(token)
    if not student_id:
        raise HTTPException(404)
    with db_session() as conn:
        data = student_portal_data(conn, student_id)
        if not data:
            raise HTTPException(404)
        return render(request, "portal.html", {"token": token, "sent": sent, **data})


@app.post("/p/{token}/renew/{package_id}")
def student_request_renewal(token: str, package_id: int):
    student_id = read_student_token(token)
    if not student_id:
        raise HTTPException(404)
    with db_session() as conn:
        try:
            request_renewal(conn, student_id, package_id)
        except ValueError:
            raise HTTPException(404)
    return RedirectResponse(f"/p/{token}?sent=1", status_code=303)


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        scope = "" if user["role"] == "admin" else "AND EXISTS (SELECT 1 FROM course_packages cp WHERE cp.student_id=st.id AND cp.teacher_id=?)"
        params = [] if user["role"] == "admin" else [user["teacher_id"]]
        students = conn.execute(f"SELECT st.* FROM students st WHERE st.active=0 {scope} ORDER BY st.name", params).fetchall()
        teachers = conn.execute("SELECT * FROM teachers WHERE active=0 ORDER BY name").fetchall() if user["role"] == "admin" else []
        return render(request, "archive.html", {"user": user, "students": students, "teachers": teachers})


@app.get("/archive/teachers/{teacher_id}", response_class=HTMLResponse)
def archived_teacher(teacher_id: int, request: Request, user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        teacher = conn.execute("SELECT * FROM teachers WHERE id=? AND active=0", (teacher_id,)).fetchone()
        if not teacher:
            raise HTTPException(404)
        packages = conn.execute("""SELECT h.*, cp.course_name, st.name AS student_name
            FROM teacher_archive_courses h JOIN course_packages cp ON cp.id=h.course_package_id
            JOIN students st ON st.id=cp.student_id WHERE h.teacher_id=? ORDER BY cp.id""", (teacher_id,)).fetchall()
        records = conn.execute("""SELECT lr.*, cp.course_name, st.name AS student_name
            FROM teacher_archive_courses h JOIN lesson_records lr ON lr.course_package_id=h.course_package_id AND lr.id<=h.last_record_id
            JOIN course_packages cp ON cp.id=lr.course_package_id JOIN students st ON st.id=cp.student_id
            WHERE h.teacher_id=? ORDER BY lr.id DESC""", (teacher_id,)).fetchall()
        return render(request, "teacher_archive.html", {"user": user, "teacher": teacher, "packages": packages, "records": records})


def archive_subject(conn, user, kind, subject_id):
    if kind == "students":
        try:
            return require_student_access(conn, user, subject_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
    if kind != "teachers":
        raise HTTPException(404)
    if user["role"] != "admin":
        raise HTTPException(403)
    subject = conn.execute("SELECT * FROM teachers WHERE id=?", (subject_id,)).fetchone()
    if not subject:
        raise HTTPException(404)
    return subject


@app.get("/archive/{kind}/{subject_id}/{action}", response_class=HTMLResponse)
def archive_confirmation(kind: str, subject_id: int, action: str, request: Request, user=Depends(get_user)):
    if action not in ("deactivate", "restore"):
        raise HTTPException(404)
    with db_session() as conn:
        subject = archive_subject(conn, user, kind, subject_id)
        balances = conn.execute("SELECT course_name, current_balance FROM course_packages WHERE student_id=? ORDER BY id", (subject_id,)).fetchall() if kind == "students" else []
        confirmation = archive_signer.dumps([user["id"], kind, subject_id, action])
        return render(request, "archive_confirm.html", {"user": user, "subject": subject, "kind": kind,
            "action": action, "balances": balances, "confirmation": confirmation})


@app.post("/archive/{kind}/{subject_id}/{action}")
def archive_action(kind: str, subject_id: int, action: str, confirmation: str = Form(...), user=Depends(get_user)):
    if action not in ("deactivate", "restore"):
        raise HTTPException(404)
    try:
        valid = archive_signer.loads(confirmation, max_age=900)
    except BadSignature:
        raise HTTPException(403, "确认已失效，请重新打开确认页")
    if valid != [user["id"], kind, subject_id, action]:
        raise HTTPException(403)
    with db_session() as conn:
        archive_subject(conn, user, kind, subject_id)
        operation = set_student_active if kind == "students" else set_teacher_active
        operation(conn, user, subject_id, action == "restore")
    return RedirectResponse("/archive" if action == "deactivate" else "/" + kind, status_code=303)


@app.get("/unassigned", response_class=HTMLResponse)
def unassigned(request: Request, error: str = "", user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        return render(request, "unassigned.html", {"user": user, "packages": list_unassigned_courses(conn),
            "teachers": conn.execute("SELECT * FROM teachers WHERE active=1 ORDER BY name").fetchall(), "error": error})


@app.post("/unassigned/{package_id}")
def assign_course_teacher(package_id: int, teacher_id: int = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        try:
            assign_teacher(conn, user, package_id, teacher_id)
        except ValueError as exc:
            return RedirectResponse(f"/unassigned?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/unassigned", status_code=303)


@app.post("/teachers/{teacher_id}/edit")
def edit_teacher(teacher_id: int, name: str = Form(""), email: str = Form(""), user=Depends(get_user)):
@app.post("/students/{student_id}/edit")
def edit_student(student_id: int, name: str = Form(""), phone: str = Form(""), email: str = Form(""), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        try:
            edit_teacher_profile(conn, user, teacher_id, name, email)
        except ValueError as exc:
            return RedirectResponse(f"/teachers?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/teachers", status_code=303)
            edit_student_profile(conn, user, student_id, name, phone, email)
        except ValueError as exc:
            return RedirectResponse(f"/students/{student_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/students/{student_id}", status_code=303)


@app.post("/students/{student_id}/packages/{package_id}/rename")
def rename_package(student_id: int, package_id: int, course_name: str = Form(""), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        if not conn.execute("SELECT 1 FROM course_packages WHERE id=? AND student_id=?", (package_id, student_id)).fetchone():
            raise HTTPException(404)
        try:
            rename_course_package(conn, user, package_id, course_name)
        except ValueError as exc:
            return RedirectResponse(f"/students/{student_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/students/{student_id}", status_code=303)


@app.post("/students/{student_id}/packages/{package_id}/adjust")
def adjust_balance(student_id: int, package_id: int, balance: str = Form(""), reason: str = Form(""), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        if not conn.execute("SELECT 1 FROM course_packages WHERE id=? AND student_id=?", (package_id, student_id)).fetchone():
            raise HTTPException(404)
        if not re.fullmatch(r"[0-9]{1,9}", balance.strip()):
            return RedirectResponse(f"/students/{student_id}?error={quote('课时必须是大于或等于 0 的整数（最多 9 位）')}", status_code=303)
        try:
            adjust_package_balance(conn, user, package_id, int(balance), reason)
        except ValueError as exc:
            return RedirectResponse(f"/students/{student_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/students/{student_id}", status_code=303)
