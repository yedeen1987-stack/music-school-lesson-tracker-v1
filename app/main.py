from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import db_session
from app.models import SCHEMA_SQL
from app.security import read_session, sign_session
from app.services import (
    add_course_package,
    authenticate,
    create_student,
    current_user,
    init_schema,
    list_today_lessons,
    record_lesson,
    seed_data,
    undo_last_record,
    update_schedule,
)

app = FastAPI(title="音乐机构课时记录 V1")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    with db_session() as conn:
        init_schema(conn, SCHEMA_SQL)
        seed_data(conn)


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
        response.set_cookie("session", sign_session(user["id"]), httponly=True, samesite="lax")
        return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/", response_class=HTMLResponse)
def today(request: Request, user=Depends(get_user)):
    with db_session() as conn:
        lessons = list_today_lessons(conn, user)
        teachers = conn.execute("SELECT * FROM teachers WHERE active = 1 ORDER BY id").fetchall()
        return render(request, "today.html", {"user": user, "lessons": lessons, "teachers": teachers, "today": date.today()})


@app.post("/lessons/{lesson_id}/{action}")
def lesson_action(lesson_id: int, action: str, user=Depends(get_user)):
    with db_session() as conn:
        record_lesson(conn, user, lesson_id, action)
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
        return render(request, "student_detail.html", {"user": user, "student": rows[0], "packages": rows, "teachers": teachers})


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
def settings(request: Request, user=Depends(get_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with db_session() as conn:
        settings_rows = conn.execute("SELECT * FROM settings").fetchall()
        logs = conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT 50").fetchall()
        return render(request, "settings.html", {"user": user, "settings": settings_rows, "logs": logs})

