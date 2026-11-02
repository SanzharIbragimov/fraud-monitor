import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp())
shutil.copy(ROOT / "config" / "rules.yaml", _tmp / "rules.yaml")
os.environ.setdefault("DB_PATH", str(_tmp / "test.db"))
os.environ.setdefault("RULES_PATH", str(_tmp / "rules.yaml"))
os.environ.setdefault("DEMO_WARMUP", "0")
os.environ.setdefault("LISTS_INBOX", str(_tmp / "inbox"))
