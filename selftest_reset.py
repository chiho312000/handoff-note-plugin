"""Self-test for the in-place handoff reset. Run with Hermes's own interpreter:
  <Hermes python.exe, the one %LOCALAPPDATA%\\hermes\\bin\\hermes.cmd launches> selftest_reset.py
Uses a temp SessionDB; never touches the live state.db."""
import importlib.util
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"))
import hermes_bootstrap  # noqa: E402,F401  (the gateway imports it first; it puts Hermes's deps on sys.path)
spec = importlib.util.spec_from_file_location("handoff_notes", HERE / "__init__.py")
hn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hn)

from hermes_state import SessionDB  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    # 1. Only the bot's "# Handoff" section is carried, not the summary or the continued mark.
    note = Path(tmp) / "n-c1.md"
    note.write_text("---\nsession: s\n---\n## Goal\nold summary\n## Handoff\nsummary's own\n"
                    "# Handoff — now\n**Next step:** run X\n\n" + hn._CONTINUED_MARK + "\n", encoding="utf-8")
    section = hn._handoff_section(note)
    assert section == "# Handoff — now\n**Next step:** run X", repr(section)
    assert hn._handoff_section(Path(tmp) / "missing.md") == ""
    assert "run X" in hn._CONTINUE_FROM_HANDOFF.format(note=note, handoff=section)

    # 2. Live rows are soft-archived (kept on disk), and the live transcript is empty.
    db = SessionDB(Path(tmp) / "state.db")
    db.create_session("s1", "telegram")
    db.append_message("s1", role="user", content="hello")
    db.append_message("s1", role="assistant", content="[CONTEXT SUMMARY]: big old summary")

    class Store:
        dirty = None
        def _db_for_session_id(self, sid): return db
        def _get_transcript_drain_lock(self): return threading.Lock()
        def _clear_dirty_transcript(self, sid): Store.dirty = sid

    assert hn._archive_live_history(Store(), "s1") is True
    assert db.get_messages("s1") == [], db.get_messages("s1")
    assert len(db.get_messages("s1", include_inactive=True)) == 2
    assert Store.dirty == "s1"
    db.close()

print("selftest_reset: OK")
