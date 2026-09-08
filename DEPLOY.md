# 部署说明

## GitHub 能不能记录数据？

GitHub 仓库负责保存代码，不能直接作为这个 FastAPI + SQLite 系统的运行数据库。

如果使用 GitHub Pages，只能展示静态 HTML，不能登录、不能完成课程、不能写入 SQLite，所以不能记录课时数据。

要记录数据，需要把代码部署到一台能运行 Python 服务的主机，例如：

- 一台办公室电脑或 Mac mini
- 云服务器
- Render / Railway / Fly.io 等应用平台

SQLite 数据库文件保存在运行服务器的 `data/music_school.sqlite3`。只要这个文件所在磁盘是持久化的，课程记录就会一直保存。

本仓库已经加入 `Procfile` 和 `render.yaml`。如果使用 Render，可以创建 Web Service 并连接这个 GitHub 仓库；`render.yaml` 会配置 Python 服务、启动命令和 1GB 持久化磁盘。

## 推荐 V1 部署方式

第一版建议：

1. GitHub 保存代码。
2. 一台固定电脑或云服务器运行 FastAPI。
3. SQLite 数据库保存在服务器本地。
4. 每天执行 `scripts/backup_sqlite.py` 生成备份。

## 通知功能

- 完成本节后立即通知学生和老师，邮件内容包含剩余课时。
- 管理员可在“基础设置”中设置余额提醒阈值，例如 `10,7,5,3,1`。
- 当完成课程后余额刚好等于阈值，系统会额外发送“课时余额提醒”。
- 未配置 SMTP 时，系统不会真正发送邮件，但会写入 `email_logs`，状态为 `disabled`。

## 启动命令

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 邮件环境变量

```bash
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USERNAME=your@email.com
export SMTP_PASSWORD=your_app_password
export SMTP_FROM=your@email.com
```

## 数据备份

```bash
python scripts/backup_sqlite.py
```

恢复时停止服务，用备份文件覆盖 `data/music_school.sqlite3`，再重新启动。
