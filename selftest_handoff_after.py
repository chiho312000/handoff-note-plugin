r"""Self-test for settings.handoff_after. Run with Hermes's own interpreter:
  "%LOCALAPPDATA%\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe" selftest_handoff_after.py
Imports only; it does not start a gateway or a CLI session.
"""
import importlib.util, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["HERMES_HOME"] = str(HERE.parents[1])  # the gamebot profile
sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"))
import hermes_bootstrap  # noqa: F401  puts Hermes's dependencies on sys.path, as the launcher does

from hermes_cli.config import load_config_readonly
from hermes_cli.plugins import _plugin_settings_entry, _nested_plugin_value

entry = _plugin_settings_entry(load_config_readonly() or {}, "handoff-notes")
live = _nested_plugin_value(entry.get("settings"), ("handoff_after",), None)
print("config.yaml settings.handoff_after =", live)
assert isinstance(live, int) and live >= 1, live


class Ctx:
    def __init__(self, value): self.value = value
    def get_config(self, key, default=None): return default if self.value is None else self.value
    def __getattr__(self, name): return lambda *a, **k: None  # register_hook / register_command


def loaded(value):
    spec = importlib.util.spec_from_file_location("handoff_notes_test", HERE / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.register(Ctx(value))
    # The same format call _instruction() makes: the prompt must state the configured count.
    assert f"contains {mod.HANDOFF_AFTER} or more" in mod._WRITE_HANDOFF.format(
        count=mod.HANDOFF_AFTER, grace=1, note="n", template="t", user_message="u")
    return mod.HANDOFF_AFTER


assert loaded(None) == 2      # key missing: default
assert loaded(5) == 5
for bad in (0, -1, "3", 2.5, True):
    assert loaded(bad) == 2, bad  # rejected, default kept
assert loaded(live) == live
print("OK")
