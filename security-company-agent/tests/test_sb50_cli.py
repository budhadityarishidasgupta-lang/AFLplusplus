import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sb50_learn


def test_cli_queue_controls_and_worker_api_handoff(tmp_path, capsys):
    db = str(tmp_path / "cli.sqlite3")
    assert sb50_learn.main(["--db", db, "enqueue", "What is uncertainty?", "--topic", "reasoning"]) == 0
    assert '"created": true' in capsys.readouterr().out
    assert sb50_learn.main(["--db", db, "pause"]) == 0
    assert '"state": "paused"' in capsys.readouterr().out
    assert sb50_learn.main(["--db", db, "once"]) == 0
    assert '"status": "paused"' in capsys.readouterr().out
    assert sb50_learn.main(["--db", db, "resume"]) == 0
    capsys.readouterr()
    assert sb50_learn.main(["--db", db, "once"]) == 3
    assert '"status": "pending_provider"' in capsys.readouterr().out
    assert sb50_learn.main(["--db", db, "status"]) == 0
    assert '"question": "What is uncertainty?"' in capsys.readouterr().out


def test_cli_empty_learning_query_does_not_claim_success(tmp_path, capsys):
    db = str(tmp_path / "empty.sqlite3")
    assert sb50_learn.main(["--db", db, "once"]) == 0
    assert '"status": "idle"' in capsys.readouterr().out
    assert sb50_learn.main(["--db", db, "learned"]) == 0
    assert capsys.readouterr().out.strip() == "[]"
