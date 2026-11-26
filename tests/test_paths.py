"""FS_DATA_DIR moves every file the app writes outside the code checkout."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_fs_data_dir_moves_all_writable_paths(tmp_path):
    code = ("from pipeline import sources, property as p, outlines\n"
            "from pipeline.ai import marks, backends\n"
            "for v in (sources.RAW, p.PROCESSED, outlines.PATH, marks.DB, backends.CALL_LOG): print(v)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True,
                         env={"FS_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}).stdout.split()
    assert len(out) == 5
    assert all(Path(v).resolve().is_relative_to(tmp_path.resolve()) for v in out), out
