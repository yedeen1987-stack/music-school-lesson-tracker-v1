from datetime import datetime
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import DATA_DIR, get_db_path


db_path = get_db_path()
backup_dir = DATA_DIR / "backups"
backup_dir.mkdir(parents=True, exist_ok=True)
target = backup_dir / f"music_school_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
shutil.copy2(db_path, target)
print(f"备份完成：{target}")
