# 部署说明（自建服务器）

## 总体结构

- GitHub 保存代码。
- 自己的服务器运行 FastAPI 服务（uvicorn + systemd），Nginx 反向代理并提供 HTTPS。
- SQLite 数据库文件放在 `/var/lib/music-school/`，不随代码更新丢失。
- 三个 systemd timer 分别负责：课前提醒入队、邮件队列兜底发送、每日备份。

## 一、准备服务器

一台 1 核 1G 的 Linux（Debian/Ubuntu）就足够两个老师使用。

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx git
sudo useradd --system --create-home --home-dir /var/lib/music-school music
sudo mkdir -p /opt/music-school /var/lib/music-school
sudo chown -R music:music /opt/music-school /var/lib/music-school
```

## 二、拉代码、装依赖

```bash
sudo -u music git clone https://github.com/yedeen1987-stack/music-school-lesson-tracker-v1.git /opt/music-school
cd /opt/music-school
sudo -u music python3 -m venv .venv
sudo -u music .venv/bin/pip install -r requirements.txt
```

## 三、配置环境变量

```bash
sudo cp /opt/music-school/.env.example /etc/music-school.env
sudo nano /etc/music-school.env
sudo chmod 600 /etc/music-school.env
```

必须修改的两项：

- `SESSION_SECRET`：用 `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` 生成。仓库里的默认值是公开的，不改等于任何人都能伪造登录 Cookie。
- `SMTP_*`：建议用 Resend / Brevo / SendGrid 的 SMTP 并配好域名 DKIM/SPF，个人邮箱群发容易进垃圾箱或被限流。

`TZ` 按机构所在时区填写，它决定“今日课程”和完成确认时间。

## 四、注册 systemd 服务和定时任务

```bash
sudo cp /opt/music-school/deploy/*.service /opt/music-school/deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-school.service
sudo systemctl enable --now music-school-reminders.timer
sudo systemctl enable --now music-school-email-queue.timer
sudo systemctl enable --now music-school-backup.timer
systemctl list-timers | grep music-school
```

`music-school-reminders.timer` 默认每天 19:00 执行，要和“基础设置”里的提醒时间保持一致；改时间时同时改这个 timer。

## 五、Nginx 和 HTTPS

```bash
sudo cp /opt/music-school/deploy/nginx.conf /etc/nginx/sites-available/music-school
sudo nano /etc/nginx/sites-available/music-school   # 改成自己的域名
sudo ln -s /etc/nginx/sites-available/music-school /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d music.yourdomain.com
```

应用只监听 `127.0.0.1:8000`，外网一律走 Nginx。

## 六、上线后立刻要做的事

1. 用 `admin / admin123` 登录，改掉默认账号密码（`users` 表目前是明文，先手工改成只有你知道的值）。
2. 在“基础设置”里确认提醒阈值，例如 `10,7,5,3,1`。
3. 发一节测试课，去“基础设置 → 邮件记录”确认状态从 `pending` 变成 `sent`。

## 七、更新代码

```bash
cd /opt/music-school
sudo -u music git pull
sudo -u music .venv/bin/pip install -r requirements.txt
sudo systemctl restart music-school
```

数据库结构变更会在服务启动时自动补齐（`app/models.py` 的 `migrate_schema`），不需要手工改表。

## 邮件是怎么发出去的

- 老师点“完成本节”时，通知只写进 `email_logs`，状态 `pending`，页面立刻返回，不等待 SMTP。
- 应用内后台线程默认每 20 秒处理一次队列（`EMAIL_WORKER_INTERVAL`）。
- 发送失败按 1 分钟、5 分钟、15 分钟退避重试，共 4 次；仍然失败才标记 `failed`。
- `music-school-email-queue.timer` 每 5 分钟兜底跑一次，防止应用重启时漏发。
- 没有配置 SMTP 时不会真的发信，记录会标记为 `disabled`，方便本地验证流程。

## 课时余额提醒规则

- 剩余课时**跌破**任一阈值就提醒一次，不要求精确等于阈值，中途撤销或手工调整也不会漏发。
- 每个阈值只提醒一次；一次掉过多个阈值只发一封。
- 续费后阈值重新武装，下次掉下去会再次提醒。

## 数据备份和恢复

`music-school-backup.timer` 每天 03:30 用 SQLite 在线备份写入 `/var/lib/music-school/backups/`，默认保留 30 份（`BACKUP_KEEP`）。

手动备份：

```bash
sudo -u music /opt/music-school/.venv/bin/python /opt/music-school/scripts/backup_sqlite.py
```

恢复：停止服务，用备份文件覆盖数据库，再启动。

```bash
sudo systemctl stop music-school
sudo -u music cp /var/lib/music-school/backups/music_school_YYYYmmdd_HHMMSS.sqlite3 /var/lib/music-school/music_school.sqlite3
sudo systemctl start music-school
```

强烈建议再把备份目录同步到另一台机器或对象存储，服务器本机的备份挡不住磁盘损坏。

## 常见排查

```bash
sudo systemctl status music-school
sudo journalctl -u music-school -n 100 --no-pager
sudo journalctl -u music-school-reminders -n 50 --no-pager
```

邮件发不出去时，先看“基础设置 → 邮件记录”里的 `error` 字段，再检查 `/etc/music-school.env` 里的 SMTP 配置。

## 其他平台

仓库仍保留 `Procfile` 和 `render.yaml`，可用于 Render 等平台。注意这类平台若没有持久化磁盘，SQLite 数据会在重新部署后丢失，所以推荐自建服务器。
