"""Exercise both documented entry points with a real child interpreter."""

import os
import sqlite3
import subprocess
import sys

from app.config import WORKSPACE_ROOT


def test_both_seed_entry_points_repeat_cleanly(tmp_path):
    path = tmp_path / "cli.db"
    environment = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{path}", USE_MOCK_AI="true")
    commands = [
        [sys.executable, "-m", "data.seed_synthetic"],
        [sys.executable, "data/seed_synthetic.py"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=WORKSPACE_ROOT, env=environment, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "10 synthetic retailers (2 active, 8 inactive), 1 synthetic route" in result.stdout
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM retailers").fetchone()[0] == 10
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT DISTINCT data_source FROM retailers").fetchall() == [("synthetic",)]
    finally:
        connection.close()
