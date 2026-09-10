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
    authenticate,
    change_password,
    list_renewal_requests,
    mark_renewal_handled,
    overdraft_limit,
    request_renewal,
    student_portal_data,
    student_portal_url,
    create_student,
    delete_student,
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
delete_confirmation = URLSafeTimedSerializer(SECRET_KEY, salt="student-delete-confirmation")


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
        undo_last_record(conn, user, record_id)
    return RedirectResponse("/records", status_code=303)


@app.get("/students", response_class=HTMLResponse)
def students(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        where = ""
        params = []
        if user["role"] == "teacher":
            where = "WHERE cp.teacher_id = ?"
            params.append(user["teacher_id"])
        rows = conn.execute(
            f"""
            SELECT st.id, st.name, st.phone, st.email, cp.course_name, cp.current_balance, t.name AS teacher_name
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
        create_student(conn, name, phone, email, course_name, teacher_id, purchased_lessons, weekday, planned_time)
    return RedirectResponse("/students", status_code=303)


@app.get("/students/{student_id}", response_class=HTMLResponse)
def student_detail(student_id: int, request: Request, user=Depends(get_user)):
    with db_session() as conn:
        teacher_clause = "AND cp.teacher_id = ?" if user["role"] == "teacher" else ""
        params = [student_id] + ([user["teacher_id"]] if user["role"] == "teacher" else [])
        rows = conn.execute(
            f"""
            SELECT st.*, cp.id AS package_id, cp.course_name, cp.current_balance, cp.purchased_lessons, t.name AS teacher_name
            FROM students st
            JOIN course_packages cp ON cp.student_id = st.id
            JOIN teachers t ON t.id = cp.teacher_id
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
                "packages": rows,
                "teachers": teachers,
                "portal_url": student_portal_url(conn, student_id),
            },
        )


@app.get("/students/{student_id}/delete", response_class=HTMLResponse)
def confirm_student_delete(student_id: int, request: Request, user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(404)
        token = delete_confirmation.dumps({"student_id": student_id, "user_id": user["id"]})
        return render(request, "student_delete.html", {"user": user, "student": student, "confirmation": token})


@app.post("/students/{student_id}/delete")
def delete_student_route(student_id: int, confirmation: str = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    try:
        data = delete_confirmation.loads(confirmation, max_age=900)
    except BadSignature:
        raise HTTPException(403, "确认已失效，请返回学生详情页重新确认")
    if data != {"student_id": student_id, "user_id": user["id"]}:
        raise HTTPException(403)
    with db_session() as conn:
        try:
            delete_student(conn, student_id)
        except ValueError:
            raise HTTPException(404)
    return RedirectResponse("/students", status_code=303)


@app.post("/students/{student_id}/packages")
def add_package(student_id: int, course_name: str = Form(...), teacher_id: int = Form(...), purchased_lessons: int = Form(...), user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons)
    return RedirectResponse(f"/students/{student_id}", status_code=303)


@app.get("/teachers", response_class=HTMLResponse)
def teachers(request: Request, user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM teachers ORDER BY id").fetchall()
        return render(request, "teachers.html", {"user": user, "teachers": rows})


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
        where = "WHERE cp.teacher_id = ?" if user["role"] == "teacher" else ""
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
        update_schedule(conn, schedule_id, teacher_id, weekday, planned_time)
    return RedirectResponse("/schedule", status_code=303)


@app.get("/records", response_class=HTMLResponse)
def records(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        where = "WHERE cp.teacher_id = ?" if user["role"] == "teacher" else ""
        params = [user["teacher_id"]] if user["role"] == "teacher" else []
        rows = conn.execute(
            f"""
            SELECT lr.*, st.name AS student_name, cp.course_name, t.name AS teacher_name
            FROM lesson_records lr
            JOIN course_packages cp ON cp.id = lr.course_package_id
            JOIN students st ON st.id = cp.student_id
            JOIN teachers t ON t.id = cp.teacher_id
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
