# 音乐机构课时记录小程序 V1

一个本地运行的极简课时记录系统。技术栈是 Python FastAPI + SQLite + Jinja2 页面，数据统一存在服务端 SQLite 数据库，不保存到每台老师手机本地。

## 功能

- 管理员可查看全部课程、学生、老师、排课、记录和邮件日志。
- 老师登录后默认只看到自己的今日课程、学生余额和记录。
- 学生登记字段：姓名、电话、Email、课程、所属老师、购买课时、固定上课日、计划时间。
- 固定排课按星期和计划时间生成今日课程。
- 今日课程只有两个操作：完成本节、跳过今天。
- 完成本节扣 1 节，并记录服务器当前确认时间。
- 跳过今天不扣课时，余额不变，也保留历史记录。
- 课时按课程包/课程维度管理，同一学生不同课程不会混用余额。
- 撤销采用反向课时流水，不删除原始历史记录。
- 老师和学生都有 Email。
- 学生凭签名链接免登录查看剩余课时和上课记录，并可提交续费意向。
- 余额为 0 时允许透支上课（可配置节数），不会卡住老师现场记录。
- 支持课程前一天提醒和改课立即通知，邮件进入队列异步发送，失败自动重试。
- 完成本节后通知学生和老师，并显示剩余课时。
- 支持自定义余额提醒阈值，例如 `10,7,5,3,1`，余额跌破阈值即提醒，每个阈值只提醒一次。
- 提供每日 SQLite 备份脚本。
- 提供基础测试覆盖核心课时规则和老师权限隔离。

## 本地启动

### Mac / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\init_db.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

## 局域网访问

启动命令已经使用 `--host 0.0.0.0`。在电脑上查看本机局域网 IP，例如 `192.168.1.23`，同一 Wi-Fi 下手机访问：

```text
http://192.168.1.23:8000
```

如果打不开，检查电脑防火墙是否允许 8000 端口。

## 默认测试账号

密码使用 PBKDF2-SHA256 哈希保存，登录后可在右上角“改密码”修改。**上线前必须改掉下面的默认密码。**

| 角色 | 账号 | 密码 |
| --- | --- | --- |
| 管理员 | admin | admin123 |
| 王老师 | wang | teacher123 |
| 李老师 | li | teacher123 |

## 邮件配置

不要把邮箱账号和密码写进代码。启动前设置环境变量：

```bash
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USERNAME=your@email.com
export SMTP_PASSWORD=your_app_password
export SMTP_FROM=your@email.com
```

Windows PowerShell：

```powershell
$env:SMTP_HOST="smtp.example.com"
$env:SMTP_PORT="587"
$env:SMTP_USERNAME="your@email.com"
$env:SMTP_PASSWORD="your_app_password"
$env:SMTP_FROM="your@email.com"
```

未配置 SMTP 时，系统不会真正发信，但仍会在 `email_logs` 中记录 `disabled` 状态，方便本地验证流程。

手动执行明日课程提醒：

```bash
python scripts/send_reminders.py
```

生产使用时用系统计划任务每分钟执行这条命令。脚本按服务的 `TZ` 时区读取“基础设置 → 课前一天提醒时间”（默认 19:00），仅在对应分钟为明天的课程入队，当天重复执行或改时间不会重复入队。实际投递由邮件队列完成，可能稍晚于设置时间。错过该分钟不补发；服务器须保持运行。已有部署需按 `DEPLOY.md` 更新 timer。

基础设置顶部显示待发送数、24 小时内最终失败数、最近成功时间及最近错误。今日课程页发现失败会提醒管理员检查设置，老师则会看到联系管理员的提示。

完成课程后，系统会把“课程完成通知”写入邮件队列，内容包含剩余课时。老师点击“完成本节”不会等待 SMTP，页面立即返回。

邮件由应用内后台线程发送（默认每 20 秒一次），失败按 1 分钟、5 分钟、15 分钟退避重试，共 4 次。也可以用 systemd timer 定时执行兜底：

```bash
python scripts/process_email_queue.py
```

管理员可以在“基础设置”中修改余额提醒阈值，例如 `10,7,5,3,1`。剩余课时**跌破**任一阈值时发送“课时余额提醒”，不要求精确等于该数字，因此撤销或手工调整都不会漏发；每个阈值只提醒一次，续费后重新生效。

## 学生自助续费

在“基础设置”里填写“对外访问地址”（例如 `https://music.yourdomain.com`）后：

- 完成通知和余额提醒邮件里会附上学生的专属链接。
- 学生打开链接不需要账号密码，可以看到各课程剩余课时和最近 20 条记录。
- 点击“我要续费”只登记意向并邮件通知老师，不做在线支付；同一课程重复点击不会重复提交。
- 老师和管理员在“续费”页面跟进，登记完课时后点“标记已处理”。

链接是用 `SESSION_SECRET` 签名的，改了密钥后旧链接会失效。管理员可以在学生详情页复制该学生的链接。

## 部署到自己的服务器

完整步骤见 [`DEPLOY.md`](DEPLOY.md)：systemd 托管服务、Nginx + HTTPS、三个定时任务（课前提醒、邮件队列兜底、每日备份）。

关键环境变量见 [`.env.example`](.env.example)，其中 `SESSION_SECRET` 必须改成随机值，否则会话 Cookie 可被伪造。

## 云端部署

仓库包含 `Procfile` 和 `render.yaml`，可用于 Render 这类支持 Python Web Service 的平台。

重点：

- GitHub 保存代码。
- 云平台运行网页服务。
- SQLite 数据需要持久化磁盘，默认配置为 `/var/data/music_school.sqlite3`。
- 邮件账号通过云平台环境变量配置。

如果云平台没有持久化磁盘，数据会在服务重启或重新部署后丢失。

## 备份和恢复

数据库文件：

```text
data/music_school.sqlite3
```

手动备份：

```bash
python scripts/backup_sqlite.py
```

备份文件会保存到：

```text
data/backups/
```

恢复时，先停止服务，再把某个备份文件复制覆盖 `data/music_school.sqlite3`，然后重新启动服务。

## 测试

```bash
pytest
```

测试覆盖：

- 完成本节扣 1
- 跳过今天扣 0
- 撤销恢复课时
- 老师权限隔离
- 当天课程从固定排课读取
- 同一学生不同课程包余额独立
- 完成通知邮件日志
- 余额提醒邮件日志
- 邮件队列入队、投递、失败重试和无 SMTP 时的降级
- 余额跳过阈值仍然提醒、同一阈值不重复提醒、续费后重新武装
- 密码哈希、遗留明文密码登录后自动升级、修改密码校验
- 透支上限、学生自助页数据隔离、续费申请去重和老师权限隔离

## 未实现项

- 没有做支付、发票、复杂补课、请假、缺席、迟到统计。
- 没有做真正的后台定时器，V1 用脚本配合系统计划任务发送提醒。
- 续费只登记意向，没有在线支付，实际课时仍由管理员在后台登记。
- 学生自助链接是长期有效的签名链接，没有做过期和吊销。
- 还没有多校区、多管理员和细粒度权限。
