const WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"];
const SESSION_COOKIE = "ms_session";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    await ensureSchema(env.DB);
    await seedData(env.DB);
    await mergeDuplicateCoursePackages(env.DB);

    try {
      if (url.pathname === "/login" && request.method === "GET") return page(request, null, loginView(url));
      if (url.pathname === "/login" && request.method === "POST") return login(request, env.DB);
      if (url.pathname === "/logout" && request.method === "POST") return redirect("/login", clearCookie());

      const user = await currentUser(request, env.DB);
      if (!user) return redirect("/login");

      if (url.pathname === "/" && request.method === "GET") return todayView(request, env.DB, user);
      if (url.pathname.startsWith("/lessons/") && request.method === "POST") return lessonAction(url, env.DB, user);
      if (url.pathname === "/students" && request.method === "GET") return studentsView(request, env.DB, user);
      if (url.pathname === "/students/new" && request.method === "GET") return studentFormView(request, env.DB, user);
      if (url.pathname === "/students" && request.method === "POST") return createStudent(request, env.DB, user);
      if (url.pathname.match(/^\/students\/\d+$/) && request.method === "GET") return studentDetailView(request, env.DB, user, Number(url.pathname.split("/")[2]));
      if (url.pathname.match(/^\/students\/\d+\/packages$/) && request.method === "POST") return addPackage(request, env.DB, user, Number(url.pathname.split("/")[2]));
      if (url.pathname === "/teachers" && request.method === "GET") return teachersView(request, env.DB, user);
      if (url.pathname === "/teachers" && request.method === "POST") return addTeacher(request, env.DB, user);
      if (url.pathname === "/schedule" && request.method === "GET") return scheduleView(request, env.DB, user);
      if (url.pathname.startsWith("/schedule/") && request.method === "POST") return updateSchedule(request, env.DB, user, Number(url.pathname.split("/")[2]));
      if (url.pathname === "/records" && request.method === "GET") return recordsView(request, env.DB, user);
      if (url.pathname.startsWith("/records/") && url.pathname.endsWith("/undo") && request.method === "POST") return undoRecord(env.DB, user, Number(url.pathname.split("/")[2]));
      if (url.pathname === "/settings" && request.method === "GET") return settingsView(request, env.DB, user);
      if (url.pathname === "/settings" && request.method === "POST") return saveSettings(request, env.DB, user);
      if (url.pathname === "/cron/reminders" && request.method === "POST") return sendTomorrowReminders(env.DB);

      return new Response("Not Found", { status: 404 });
    } catch (error) {
      return page(request, null, `<section class="card"><h1>出错了</h1><p>${escapeHtml(error.message)}</p><a class="button" href="/">返回</a></section>`, 500);
    }
  },
};

async function ensureSchema(db) {
  const statements = SCHEMA_SQL.split(";").map((s) => s.trim()).filter(Boolean);
  for (const sql of statements) await db.prepare(sql).run();
}

async function seedData(db) {
  const exists = await db.prepare("SELECT COUNT(*) AS count FROM users").first();
  await db.prepare("INSERT OR IGNORE INTO settings (key, value) VALUES ('reminder_time', '19:00')").run();
  await db.prepare("INSERT OR IGNORE INTO settings (key, value) VALUES ('low_balance_thresholds', '10,7,5,3,1')").run();
  if (exists.count > 0) return;
  await db.prepare("INSERT INTO teachers (name, email) VALUES (?, ?)").bind("王老师", "wang@example.com").run();
  await db.prepare("INSERT INTO teachers (name, email) VALUES (?, ?)").bind("李老师", "li@example.com").run();
  const wang = await db.prepare("SELECT id FROM teachers WHERE name = ?").bind("王老师").first();
  const li = await db.prepare("SELECT id FROM teachers WHERE name = ?").bind("李老师").first();
  await db.prepare("INSERT INTO users (username, password, role) VALUES ('admin', 'admin123', 'admin')").run();
  await db.prepare("INSERT INTO users (username, password, role, teacher_id) VALUES ('wang', 'teacher123', 'teacher', ?)").bind(wang.id).run();
  await db.prepare("INSERT INTO users (username, password, role, teacher_id) VALUES ('li', 'teacher123', 'teacher', ?)").bind(li.id).run();
  await createStudentRows(db, { name: "Maria", phone: "11 99999-9999", email: "maria@example.com", course_name: "钢琴", teacher_id: wang.id, purchased_lessons: 10, weekday: 1, planned_time: "14:00" });
  await createStudentRows(db, { name: "Lucas", phone: "11 98888-8888", email: "lucas@example.com", course_name: "吉他", teacher_id: li.id, purchased_lessons: 8, weekday: 1, planned_time: "16:00" });
}

async function mergeDuplicateCoursePackages(db) {
  const groups = await db.prepare(`
    SELECT student_id, course_name, teacher_id, MIN(id) AS keep_id, COUNT(*) AS count,
           SUM(purchased_lessons) AS purchased_total, SUM(current_balance) AS balance_total
    FROM course_packages
    WHERE active = 1
    GROUP BY student_id, course_name, teacher_id
    HAVING COUNT(*) > 1
  `).all();
  for (const group of groups.results) {
    const duplicates = await db.prepare(`
      SELECT id FROM course_packages
      WHERE student_id = ? AND course_name = ? AND teacher_id = ? AND active = 1 AND id != ?
    `).bind(group.student_id, group.course_name, group.teacher_id, group.keep_id).all();
    for (const row of duplicates.results) {
      await db.prepare("UPDATE schedules SET course_package_id = ? WHERE course_package_id = ?").bind(group.keep_id, row.id).run();
      await db.prepare("UPDATE lesson_records SET course_package_id = ? WHERE course_package_id = ?").bind(group.keep_id, row.id).run();
      await db.prepare("UPDATE lesson_transactions SET course_package_id = ? WHERE course_package_id = ?").bind(group.keep_id, row.id).run();
      await db.prepare("DELETE FROM course_packages WHERE id = ?").bind(row.id).run();
    }
    await db.prepare("UPDATE course_packages SET purchased_lessons = ?, current_balance = ? WHERE id = ?").bind(group.purchased_total, group.balance_total, group.keep_id).run();
  }
}

async function login(request, db) {
  const form = await request.formData();
  const user = await db.prepare("SELECT * FROM users WHERE username = ? AND password = ?").bind(form.get("username"), form.get("password")).first();
  if (!user) return redirect("/login?error=1");
  return redirect("/", cookieFor(user.id));
}

async function currentUser(request, db) {
  const cookies = parseCookies(request.headers.get("Cookie") || "");
  const id = Number(cookies[SESSION_COOKIE] || 0);
  if (!id) return null;
  return db.prepare("SELECT * FROM users WHERE id = ?").bind(id).first();
}

async function todayView(request, db, user) {
  await ensureTodayInstances(db);
  const today = localDate();
  const filter = user.role === "teacher" ? "AND cp.teacher_id = ?" : "";
  const stmt = db.prepare(`
    SELECT li.id, li.status, li.confirmed_at, s.planned_time, st.name AS student_name,
           cp.course_name, cp.current_balance, t.name AS teacher_name
    FROM lesson_instances li
    JOIN schedules s ON s.id = li.schedule_id
    JOIN course_packages cp ON cp.id = s.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE li.lesson_date = ? ${filter}
    ORDER BY s.planned_time, st.name
  `);
  const rows = user.role === "teacher" ? await stmt.bind(today, user.teacher_id).all() : await stmt.bind(today).all();
  return page(request, user, `
    <section class="heading"><div><h1>今日课程</h1><p>${today}</p></div><span class="pill">${user.role === "admin" ? "管理员" : "老师"}</span></section>
    <div class="list">${rows.results.map(lessonCard).join("") || `<p class="empty">今天没有固定课程</p>`}</div>
  `);
}

function lessonCard(row) {
  const status = { planned: "待处理", completed: "已完成", skipped: "已跳过" }[row.status];
  const actions = row.status === "planned" ? `
    <div class="actions">
      <form method="post" action="/lessons/${row.id}/complete"><button class="primary">完成本节</button></form>
      <form method="post" action="/lessons/${row.id}/skip"><button>跳过今天</button></form>
    </div>` : "";
  return `<article class="card"><div class="row"><strong>${row.planned_time}</strong><span class="status">${status}</span></div><h2>${escapeHtml(row.student_name)} · ${escapeHtml(row.course_name)}</h2><p>${escapeHtml(row.teacher_name)} · 剩余 ${row.current_balance} 节</p>${actions}</article>`;
}

async function lessonAction(url, db, user) {
  const [, , id, action] = url.pathname.split("/");
  await recordLesson(db, user, Number(id), action);
  return redirect("/");
}

async function recordLesson(db, user, instanceId, action) {
  if (!["complete", "skip"].includes(action)) throw new Error("操作无效");
  if (!(await canAccessInstance(db, user, instanceId))) throw new Error("无权操作这节课");
  const row = await db.prepare(`
    SELECT li.status, s.course_package_id, cp.current_balance
    FROM lesson_instances li
    JOIN schedules s ON s.id = li.schedule_id
    JOIN course_packages cp ON cp.id = s.course_package_id
    WHERE li.id = ?
  `).bind(instanceId).first();
  if (!row) throw new Error("课程不存在");
  if (row.status !== "planned") throw new Error("这节课已处理");
  const delta = action === "complete" ? -1 : 0;
  const newBalance = row.current_balance + delta;
  if (newBalance < 0) throw new Error("剩余课时不足");
  const status = action === "complete" ? "completed" : "skipped";
  const confirmedAt = new Date().toISOString();
  await db.batch([
    db.prepare("UPDATE course_packages SET current_balance = ? WHERE id = ?").bind(newBalance, row.course_package_id),
    db.prepare("UPDATE lesson_instances SET status = ?, confirmed_at = ? WHERE id = ?").bind(status, confirmedAt, instanceId),
  ]);
  const inserted = await db.prepare(`
    INSERT INTO lesson_records (lesson_instance_id, course_package_id, action, delta_lessons, balance_after, actor_user_id)
    VALUES (?, ?, ?, ?, ?, ?)
  `).bind(instanceId, row.course_package_id, action, delta, newBalance, user.id).run();
  const recordId = inserted.meta.last_row_id;
  await db.prepare("INSERT INTO lesson_transactions (course_package_id, lesson_record_id, delta_lessons, reason) VALUES (?, ?, ?, ?)").bind(row.course_package_id, recordId, delta, action).run();
  if (action === "complete") {
    await notifyLessonCompleted(db, instanceId, newBalance);
    await notifyLowBalanceIfNeeded(db, row.course_package_id, newBalance);
  }
}

async function studentsView(request, db, user) {
  const filter = user.role === "teacher" ? "WHERE cp.teacher_id = ?" : "";
  const stmt = db.prepare(`
    SELECT st.id, st.name, st.phone, st.email, cp.course_name, cp.current_balance, t.name AS teacher_name
    FROM students st
    JOIN course_packages cp ON cp.student_id = st.id
    JOIN teachers t ON t.id = cp.teacher_id
    ${filter}
    ORDER BY st.name
  `);
  const rows = user.role === "teacher" ? await stmt.bind(user.teacher_id).all() : await stmt.all();
  return page(request, user, `<section class="heading"><h1>学生</h1>${user.role === "admin" ? `<a class="button" href="/students/new">新增学生</a>` : ""}</section><div class="list">${rows.results.map((s) => `<a class="card link" href="/students/${s.id}"><h2>${escapeHtml(s.name)}</h2><p>${escapeHtml(s.course_name)} · ${escapeHtml(s.teacher_name)}</p><p>剩余 ${s.current_balance} 节</p></a>`).join("") || `<p class="empty">暂无学生</p>`}</div>`);
}

async function studentFormView(request, db, user) {
  requireAdmin(user);
  const teachers = await db.prepare("SELECT * FROM teachers WHERE active = 1 ORDER BY id").all();
  return page(request, user, `<h1>新增学生</h1><form method="post" action="/students" class="form">${studentFields(teachers.results)}<button class="primary">保存</button></form>`);
}

function studentFields(teachers) {
  return `
    <label>姓名 *<input name="name" required></label>
    <label>电话<input name="phone"></label>
    <label>Email *<input name="email" type="email" required></label>
    <label>课程 *<input name="course_name" required value="钢琴"></label>
    <label>所属老师 *<select name="teacher_id">${teachers.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("")}</select></label>
    <label>购买课时 *<input name="purchased_lessons" type="number" min="0" required value="10"></label>
    <label>固定上课日 *<select name="weekday">${WEEKDAYS.map((d, i) => `<option value="${i}">${d}</option>`).join("")}</select></label>
    <label>计划时间 *<input name="planned_time" type="time" required value="14:00"></label>`;
}

async function createStudent(request, db, user) {
  requireAdmin(user);
  const form = await request.formData();
  await createStudentRows(db, Object.fromEntries(form.entries()));
  return redirect("/students");
}

async function createStudentRows(db, data) {
  const student = await db.prepare("INSERT INTO students (name, phone, email) VALUES (?, ?, ?)").bind(data.name, data.phone || "", data.email).run();
  const studentId = student.meta.last_row_id;
  const packageId = await addCoursePackageRows(db, studentId, data.course_name, Number(data.teacher_id), Number(data.purchased_lessons));
  await db.prepare("INSERT INTO schedules (course_package_id, weekday, planned_time) VALUES (?, ?, ?)").bind(packageId, Number(data.weekday), data.planned_time).run();
  return studentId;
}

async function addCoursePackageRows(db, studentId, courseName, teacherId, lessons) {
  const existing = await db.prepare(`
    SELECT id, purchased_lessons, current_balance
    FROM course_packages
    WHERE student_id = ? AND course_name = ? AND teacher_id = ? AND active = 1
    ORDER BY id
    LIMIT 1
  `).bind(studentId, courseName, teacherId).first();
  if (existing) {
    const purchased = existing.purchased_lessons + lessons;
    const balance = existing.current_balance + lessons;
    await db.prepare("UPDATE course_packages SET purchased_lessons = ?, current_balance = ? WHERE id = ?").bind(purchased, balance, existing.id).run();
    await db.prepare("INSERT INTO lesson_transactions (course_package_id, delta_lessons, reason) VALUES (?, ?, 'purchase')").bind(existing.id, lessons).run();
    return existing.id;
  }
  const inserted = await db.prepare(`
    INSERT INTO course_packages (student_id, course_name, teacher_id, purchased_lessons, current_balance)
    VALUES (?, ?, ?, ?, ?)
  `).bind(studentId, courseName, teacherId, lessons, lessons).run();
  const packageId = inserted.meta.last_row_id;
  await db.prepare("INSERT INTO lesson_transactions (course_package_id, delta_lessons, reason) VALUES (?, ?, 'purchase')").bind(packageId, lessons).run();
  return packageId;
}

async function studentDetailView(request, db, user, studentId) {
  const filter = user.role === "teacher" ? "AND cp.teacher_id = ?" : "";
  const stmt = db.prepare(`
    SELECT st.*, cp.id AS package_id, cp.course_name, cp.current_balance, cp.purchased_lessons, t.name AS teacher_name
    FROM students st
    JOIN course_packages cp ON cp.student_id = st.id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE st.id = ? ${filter}
  `);
  const rows = user.role === "teacher" ? await stmt.bind(studentId, user.teacher_id).all() : await stmt.bind(studentId).all();
  if (!rows.results.length) throw new Error("学生不存在");
  const teachers = await db.prepare("SELECT * FROM teachers WHERE active = 1 ORDER BY id").all();
  const first = rows.results[0];
  return page(request, user, `
    <section class="heading"><h1>${escapeHtml(first.name)}</h1></section>
    <div class="card"><p>电话：${escapeHtml(first.phone || "-")}</p><p>Email：${escapeHtml(first.email)}</p></div>
    <h2 class="subhead">课程包</h2><div class="list">${rows.results.map((p) => `<article class="card"><h2>${escapeHtml(p.course_name)}</h2><p>${escapeHtml(p.teacher_name)} · 累计购买 ${p.purchased_lessons} 节 · 剩余 ${p.current_balance} 节</p></article>`).join("")}</div>
    ${user.role === "admin" ? `<h2 class="subhead">续费课时</h2><form method="post" action="/students/${studentId}/packages" class="form"><label>课程<input name="course_name" required></label><label>老师<select name="teacher_id">${teachers.results.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("")}</select></label><label>购买课时<input name="purchased_lessons" type="number" min="0" value="10"></label><button class="primary">增加</button></form>` : ""}
  `);
}

async function addPackage(request, db, user, studentId) {
  requireAdmin(user);
  const form = await request.formData();
  await addCoursePackageRows(db, studentId, form.get("course_name"), Number(form.get("teacher_id")), Number(form.get("purchased_lessons")));
  return redirect(`/students/${studentId}`);
}

async function teachersView(request, db, user) {
  requireAdmin(user);
  const rows = await db.prepare("SELECT * FROM teachers ORDER BY id").all();
  return page(request, user, `<h1>老师</h1><div class="list">${rows.results.map((t) => `<article class="card"><h2>${escapeHtml(t.name)}</h2><p>${escapeHtml(t.email)}</p></article>`).join("")}</div><form method="post" action="/teachers" class="form"><label>姓名<input name="name" required></label><label>Email<input name="email" type="email" required></label><button class="primary">新增老师</button></form>`);
}

async function addTeacher(request, db, user) {
  requireAdmin(user);
  const form = await request.formData();
  await db.prepare("INSERT INTO teachers (name, email) VALUES (?, ?)").bind(form.get("name"), form.get("email")).run();
  return redirect("/teachers");
}

async function scheduleView(request, db, user) {
  const filter = user.role === "teacher" ? "WHERE cp.teacher_id = ?" : "";
  const stmt = db.prepare(`
    SELECT s.*, st.name AS student_name, cp.course_name, cp.teacher_id, t.name AS teacher_name
    FROM schedules s
    JOIN course_packages cp ON cp.id = s.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    ${filter}
    ORDER BY s.weekday, s.planned_time
  `);
  const rows = user.role === "teacher" ? await stmt.bind(user.teacher_id).all() : await stmt.all();
  const teachers = await db.prepare("SELECT * FROM teachers WHERE active = 1 ORDER BY id").all();
  return page(request, user, `<h1>固定排课</h1><div class="list">${rows.results.map((item) => scheduleCard(item, teachers.results, user)).join("") || `<p class="empty">暂无排课</p>`}</div>`);
}

function scheduleCard(item, teachers, user) {
  const form = user.role === "admin" ? `<form method="post" action="/schedule/${item.id}" class="inline-form"><select name="weekday">${WEEKDAYS.map((d, i) => `<option value="${i}" ${i === item.weekday ? "selected" : ""}>${d}</option>`).join("")}</select><input name="planned_time" type="time" value="${item.planned_time}"><select name="teacher_id">${teachers.map((t) => `<option value="${t.id}" ${t.id === item.teacher_id ? "selected" : ""}>${escapeHtml(t.name)}</option>`).join("")}</select><button>保存</button></form>` : "";
  return `<article class="card"><h2>${WEEKDAYS[item.weekday]} ${item.planned_time}</h2><p>${escapeHtml(item.student_name)} · ${escapeHtml(item.course_name)} · ${escapeHtml(item.teacher_name)}</p>${form}</article>`;
}

async function updateSchedule(request, db, user, scheduleId) {
  requireAdmin(user);
  const form = await request.formData();
  await db.prepare("UPDATE course_packages SET teacher_id = ? WHERE id = (SELECT course_package_id FROM schedules WHERE id = ?)").bind(Number(form.get("teacher_id")), scheduleId).run();
  await db.prepare("UPDATE schedules SET weekday = ?, planned_time = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?").bind(Number(form.get("weekday")), form.get("planned_time"), scheduleId).run();
  await notifyScheduleChanged(db, scheduleId);
  return redirect("/schedule");
}

async function recordsView(request, db, user) {
  const filter = user.role === "teacher" ? "WHERE cp.teacher_id = ?" : "";
  const stmt = db.prepare(`
    SELECT lr.*, st.name AS student_name, cp.course_name, t.name AS teacher_name
    FROM lesson_records lr
    JOIN course_packages cp ON cp.id = lr.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    ${filter}
    ORDER BY lr.id DESC
    LIMIT 100
  `);
  const rows = user.role === "teacher" ? await stmt.bind(user.teacher_id).all() : await stmt.all();
  return page(request, user, `<h1>记录</h1><div class="list">${rows.results.map((r) => `<article class="card"><div class="row"><strong>${escapeHtml(r.student_name)} · ${escapeHtml(r.course_name)}</strong><span>${r.created_at}</span></div><p>${escapeHtml(r.teacher_name)} · ${r.action} · ${r.delta_lessons} 节 · 余额 ${r.balance_after}</p>${["complete", "skip"].includes(r.action) ? `<form method="post" action="/records/${r.id}/undo"><button>撤销</button></form>` : ""}</article>`).join("") || `<p class="empty">暂无记录</p>`}</div>`);
}

async function undoRecord(db, user, recordId) {
  const record = await db.prepare("SELECT * FROM lesson_records WHERE id = ?").bind(recordId).first();
  if (!record) throw new Error("记录不存在");
  if (!(await canAccessInstance(db, user, record.lesson_instance_id))) throw new Error("无权撤销");
  const packageRow = await db.prepare("SELECT current_balance FROM course_packages WHERE id = ?").bind(record.course_package_id).first();
  const reverseDelta = -record.delta_lessons;
  const newBalance = packageRow.current_balance + reverseDelta;
  const reverseAction = record.action === "complete" ? "undo_complete" : "undo_skip";
  await db.prepare("UPDATE course_packages SET current_balance = ? WHERE id = ?").bind(newBalance, record.course_package_id).run();
  await db.prepare("UPDATE lesson_instances SET status = 'planned', confirmed_at = NULL WHERE id = ?").bind(record.lesson_instance_id).run();
  const inserted = await db.prepare(`
    INSERT INTO lesson_records (lesson_instance_id, course_package_id, action, delta_lessons, balance_after, actor_user_id, note)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `).bind(record.lesson_instance_id, record.course_package_id, reverseAction, reverseDelta, newBalance, user.id, `撤销记录 #${recordId}`).run();
  await db.prepare("INSERT INTO lesson_transactions (course_package_id, lesson_record_id, delta_lessons, reason) VALUES (?, ?, ?, ?)").bind(record.course_package_id, inserted.meta.last_row_id, reverseDelta, reverseAction).run();
  return redirect("/records");
}

async function settingsView(request, db, user) {
  requireAdmin(user);
  const settings = await db.prepare("SELECT * FROM settings").all();
  const map = Object.fromEntries(settings.results.map((s) => [s.key, s.value]));
  const logs = await db.prepare("SELECT * FROM email_logs ORDER BY id DESC LIMIT 50").all();
  return page(request, user, `
    <h1>基础设置</h1>
    <div class="card"><form method="post" action="/settings" class="form">
      <label>课前一天提醒时间<input name="reminder_time" type="time" value="${escapeHtml(map.reminder_time || "19:00")}"></label>
      <label>余额提醒<input name="low_balance_thresholds" value="${escapeHtml(map.low_balance_thresholds || "10,7,5,3,1")}"></label>
      <button class="primary">保存设置</button>
    </form><p>云端邮件发送需要接入 HTTP 邮件服务。当前会先完整记录通知日志。</p></div>
    <h2 class="subhead">通知记录</h2><div class="list">${logs.results.map((l) => `<article class="card"><div class="row"><strong>${escapeHtml(l.recipient_email)}</strong><span class="status">${l.status}</span></div><p>${escapeHtml(l.subject)} · ${l.created_at}</p>${l.error ? `<p class="error">${escapeHtml(l.error)}</p>` : ""}</article>`).join("") || `<p class="empty">暂无通知记录</p>`}</div>
  `);
}

async function saveSettings(request, db, user) {
  requireAdmin(user);
  const form = await request.formData();
  await db.prepare("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)").bind("reminder_time", form.get("reminder_time")).run();
  await db.prepare("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)").bind("low_balance_thresholds", form.get("low_balance_thresholds")).run();
  return redirect("/settings");
}

async function canAccessInstance(db, user, instanceId) {
  if (user.role === "admin") return true;
  const row = await db.prepare(`
    SELECT cp.teacher_id
    FROM lesson_instances li
    JOIN schedules s ON s.id = li.schedule_id
    JOIN course_packages cp ON cp.id = s.course_package_id
    WHERE li.id = ?
  `).bind(instanceId).first();
  return row && row.teacher_id === user.teacher_id;
}

async function ensureTodayInstances(db) {
  const weekday = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
  const today = localDate();
  const schedules = await db.prepare("SELECT id FROM schedules WHERE active = 1 AND weekday = ?").bind(weekday).all();
  for (const schedule of schedules.results) {
    await db.prepare("INSERT OR IGNORE INTO lesson_instances (schedule_id, lesson_date) VALUES (?, ?)").bind(schedule.id, today).run();
  }
}

async function notifyLessonCompleted(db, lessonInstanceId, balanceAfter) {
  const lesson = await db.prepare(`
    SELECT st.name AS student_name, st.email AS student_email,
           t.name AS teacher_name, t.email AS teacher_email, cp.course_name, li.confirmed_at
    FROM lesson_instances li
    JOIN schedules s ON s.id = li.schedule_id
    JOIN course_packages cp ON cp.id = s.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE li.id = ?
  `).bind(lessonInstanceId).first();
  if (!lesson) return;
  const body = `课程已完成\n\n学生：${lesson.student_name}\n课程：${lesson.course_name}\n老师：${lesson.teacher_name}\n确认时间：${lesson.confirmed_at}\n剩余课时：${balanceAfter} 节\n`;
  await logEmail(db, lesson.student_email, "student", "课程完成通知", body, "lesson_instance", lessonInstanceId);
  await logEmail(db, lesson.teacher_email, "teacher", "课程完成通知", body, "lesson_instance", lessonInstanceId);
}

async function notifyLowBalanceIfNeeded(db, packageId, balanceAfter) {
  const row = await db.prepare("SELECT value FROM settings WHERE key = 'low_balance_thresholds'").first();
  const thresholds = parseThresholds(row?.value || "10,7,5,3,1");
  if (!thresholds.has(balanceAfter)) return;
  const pack = await db.prepare(`
    SELECT st.name AS student_name, st.email AS student_email,
           t.name AS teacher_name, t.email AS teacher_email, cp.course_name
    FROM course_packages cp
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE cp.id = ?
  `).bind(packageId).first();
  const body = `课时余额提醒\n\n学生：${pack.student_name}\n课程：${pack.course_name}\n老师：${pack.teacher_name}\n当前剩余：${balanceAfter} 节\n`;
  await logEmail(db, pack.student_email, "student", "课时余额提醒", body, "course_package", packageId);
  await logEmail(db, pack.teacher_email, "teacher", "课时余额提醒", body, "course_package", packageId);
}

async function notifyScheduleChanged(db, scheduleId) {
  const lesson = await db.prepare(`
    SELECT s.weekday, s.planned_time, st.name AS student_name, st.email AS student_email,
           t.name AS teacher_name, t.email AS teacher_email, cp.course_name
    FROM schedules s
    JOIN course_packages cp ON cp.id = s.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE s.id = ?
  `).bind(scheduleId).first();
  const body = `课程时间已更新\n\n学生：${lesson.student_name}\n课程：${lesson.course_name}\n老师：${lesson.teacher_name}\n固定时间：${WEEKDAYS[lesson.weekday]} ${lesson.planned_time}\n`;
  await logEmail(db, lesson.student_email, "student", "课程时间更新", body, "schedule", scheduleId);
  await logEmail(db, lesson.teacher_email, "teacher", "课程时间更新", body, "schedule", scheduleId);
}

async function sendTomorrowReminders(db) {
  const tomorrow = new Date(Date.now() + 86400000);
  const weekday = tomorrow.getDay() === 0 ? 6 : tomorrow.getDay() - 1;
  const rows = await db.prepare(`
    SELECT s.id, s.planned_time, st.name AS student_name, st.email AS student_email,
           t.name AS teacher_name, t.email AS teacher_email, cp.course_name
    FROM schedules s
    JOIN course_packages cp ON cp.id = s.course_package_id
    JOIN students st ON st.id = cp.student_id
    JOIN teachers t ON t.id = cp.teacher_id
    WHERE s.active = 1 AND s.reminder_enabled = 1 AND s.weekday = ?
  `).bind(weekday).all();
  for (const row of rows.results) {
    const body = `明天课程提醒\n\n学生：${row.student_name}\n课程：${row.course_name}\n老师：${row.teacher_name}\n时间：${row.planned_time}\n`;
    await logEmail(db, row.student_email, "student", "明天课程提醒", body, "schedule", row.id);
    await logEmail(db, row.teacher_email, "teacher", "明天课程提醒", body, "schedule", row.id);
  }
  return new Response(JSON.stringify({ ok: true, count: rows.results.length }), { headers: { "content-type": "application/json" } });
}

async function logEmail(db, email, type, subject, body, relatedType, relatedId) {
  await db.prepare(`
    INSERT INTO email_logs (recipient_email, recipient_type, subject, body, status, error, related_type, related_id)
    VALUES (?, ?, ?, ?, 'disabled', NULL, ?, ?)
  `).bind(email, type, subject, body, relatedType, relatedId).run();
}

function parseThresholds(value) {
  return new Set(String(value).replaceAll("，", ",").split(",").map((x) => Number(x.trim())).filter((x) => Number.isInteger(x)));
}

function requireAdmin(user) {
  if (user.role !== "admin") throw new Error("需要管理员权限");
}

function parseCookies(header) {
  return Object.fromEntries(header.split(";").map((part) => part.trim().split("=")).filter((x) => x.length === 2));
}

function cookieFor(id) {
  return { "Set-Cookie": `${SESSION_COOKIE}=${id}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=604800` };
}

function clearCookie() {
  return { "Set-Cookie": `${SESSION_COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0` };
}

function redirect(path, headers = {}) {
  return new Response(null, { status: 303, headers: { Location: path, ...headers } });
}

function localDate() {
  return new Date().toISOString().slice(0, 10);
}

function page(request, user, content, status = 200) {
  return new Response(`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>课时记录</title>
  <style>${CSS}</style>
</head>
<body>
  ${user ? nav(user) : ""}
  <main class="page">${content}</main>
</body>
</html>`, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

function loginView(url) {
  return `<section class="login"><h1>登录</h1>${url.searchParams.get("error") ? `<p class="error">账号或密码不正确</p>` : ""}<form method="post" action="/login" class="form"><label>账号<input name="username" required></label><label>密码<input name="password" type="password" required></label><button class="primary">登录</button></form></section>`;
}

function nav(user) {
  return `<header class="topbar"><a class="brand" href="/">课时记录</a><form method="post" action="/logout"><button class="link-button">退出</button></form></header><nav class="tabs"><a href="/">今日</a><a href="/students">学生</a><a href="/schedule">排课</a><a href="/records">记录</a>${user.role === "admin" ? `<a href="/teachers">老师</a><a href="/settings">设置</a>` : ""}</nav>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

const CSS = `
:root{--bg:#f7f7f4;--text:#202124;--muted:#656b72;--line:#deded8;--brand:#116b5f;--accent:#c35a2e;--card:#fff}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text)}a{color:inherit;text-decoration:none}.topbar,.tabs,.page{max-width:860px;margin:0 auto}.topbar{height:56px;display:flex;align-items:center;justify-content:space-between;padding:0 16px}.brand{font-weight:800;font-size:18px}.tabs{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:6px;padding:0 12px 12px;overflow-x:auto}.tabs a,.button,button{border:1px solid var(--line);background:#fff;min-height:40px;border-radius:8px;padding:9px 12px;font-size:15px;text-align:center}.page{padding:16px}h1{font-size:26px;margin:8px 0 18px}h2{font-size:17px;margin:0 0 6px}p{margin:4px 0;color:var(--muted);line-height:1.45}.heading,.row,.actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.list{display:grid;gap:10px}.card,.login{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}.link{display:block}.status,.pill{display:inline-flex;align-items:center;min-height:28px;border-radius:999px;padding:3px 10px;background:#eef4ee;color:var(--brand);font-size:13px}.actions{justify-content:flex-start;margin-top:12px}.primary{background:var(--brand);border-color:var(--brand);color:white;font-weight:700}.link-button{border:0;background:transparent;color:var(--muted)}.form{display:grid;gap:12px;max-width:520px}label{display:grid;gap:6px;font-weight:700}input,select{width:100%;min-height:42px;border:1px solid var(--line);border-radius:8px;padding:9px 10px;font-size:16px;background:white}.inline-form{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.inline-form button{grid-column:1/-1}.subhead{margin:22px 0 10px}.empty{padding:18px 4px}.error{color:var(--accent)}@media(min-width:720px){.tabs{display:flex}.tabs a{min-width:92px}.list{grid-template-columns:repeat(2,minmax(0,1fr))}.login{max-width:420px;margin:64px auto}}`;

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,email TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,password TEXT NOT NULL,role TEXT NOT NULL CHECK (role IN ('admin','teacher')),teacher_id INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY (teacher_id) REFERENCES teachers(id));
CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT,email TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS course_packages (id INTEGER PRIMARY KEY AUTOINCREMENT,student_id INTEGER NOT NULL,course_name TEXT NOT NULL,teacher_id INTEGER NOT NULL,purchased_lessons INTEGER NOT NULL CHECK (purchased_lessons >= 0),current_balance INTEGER NOT NULL CHECK (current_balance >= 0),active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY (student_id) REFERENCES students(id),FOREIGN KEY (teacher_id) REFERENCES teachers(id));
CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT,course_package_id INTEGER NOT NULL,weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),planned_time TEXT NOT NULL,reminder_enabled INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY (course_package_id) REFERENCES course_packages(id));
CREATE TABLE IF NOT EXISTS lesson_instances (id INTEGER PRIMARY KEY AUTOINCREMENT,schedule_id INTEGER NOT NULL,lesson_date TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','completed','skipped')),confirmed_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(schedule_id,lesson_date),FOREIGN KEY (schedule_id) REFERENCES schedules(id));
CREATE TABLE IF NOT EXISTS lesson_records (id INTEGER PRIMARY KEY AUTOINCREMENT,lesson_instance_id INTEGER NOT NULL,course_package_id INTEGER NOT NULL,action TEXT NOT NULL CHECK (action IN ('complete','skip','undo_complete','undo_skip','adjust')),delta_lessons INTEGER NOT NULL,balance_after INTEGER NOT NULL,actor_user_id INTEGER,note TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY (lesson_instance_id) REFERENCES lesson_instances(id),FOREIGN KEY (course_package_id) REFERENCES course_packages(id),FOREIGN KEY (actor_user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS lesson_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,course_package_id INTEGER NOT NULL,lesson_record_id INTEGER,delta_lessons INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY (course_package_id) REFERENCES course_packages(id),FOREIGN KEY (lesson_record_id) REFERENCES lesson_records(id));
CREATE TABLE IF NOT EXISTS email_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,recipient_email TEXT NOT NULL,recipient_type TEXT NOT NULL CHECK (recipient_type IN ('student','teacher')),subject TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','disabled')),error TEXT,related_type TEXT,related_id INTEGER,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_course_packages_teacher ON course_packages(teacher_id);
CREATE INDEX IF NOT EXISTS idx_schedules_weekday ON schedules(weekday);
CREATE INDEX IF NOT EXISTS idx_lesson_instances_date ON lesson_instances(lesson_date);
CREATE INDEX IF NOT EXISTS idx_email_logs_created ON email_logs(created_at);
`;
