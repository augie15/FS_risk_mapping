"""Where the app keeps its data.

`FS_DATA_DIR` (set in the environment, e.g. by the systemd unit) moves everything the app writes
outside the code checkout; the default is `data/` in the checkout.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ["FS_DATA_DIR"]).resolve() if os.getenv("FS_DATA_DIR") else ROOT / "data"
