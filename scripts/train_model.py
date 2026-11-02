"""Предобучение модели холодного старта на синтетическом потоке.

    python scripts/train_model.py     # → models/model.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ml  # noqa: E402
from app.evaluate import train  # noqa: E402

if __name__ == "__main__":
    m = train()
    ml.save(m)
    print("model:", m.version, m.meta)
    for name, c in sorted(zip(ml.NAMES, m.coef), key=lambda x: -abs(x[1])):
        print(f"{name:26s} {c:+.2f}")
