"""备份 SQLite 数据库。备份目录默认与数据库文件同级，可用 BACKUP_DIR 覆盖。"""

from datetime import datetime
from pathlib import Path
import os
import sqlite3
import sys
import subprocess
import logging

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import get_db_path


def upload_backup(target):
    remote = os.getenv('BACKUP_RCLONE_REMOTE', '').strip()
    if not remote:
        return
    try:
        subprocess.run(['rclone', 'copy', str(target), remote], check=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error('异地备份上传失败，本地备份已保留：%s；原因：%s', target, exc)
    else:
        print(f'异地备份上传完成：{target.name}')


def main():
    db_path = get_db_path()
    backup_dir = Path(os.getenv("BACKUP_DIR", str(db_path.parent / "backups")))
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"music_school_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
    # 用 SQLite 自带的在线备份，服务运行中也能安全备份。
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    keep = int(os.getenv("BACKUP_KEEP", "30"))
    backups = sorted(backup_dir.glob("music_school_*.sqlite3"))
    for old in backups[:-keep]:
        old.unlink()
    print(f"备份完成：{target}")
    upload_backup(target)
    return target


if __name__ == '__main__':
    main()
