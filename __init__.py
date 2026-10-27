"""Save each context-compression summary as a handoff note.

The summarizer runs as an auxiliary LLM call with aux_task="compression", and
post_auxiliary_call carries its reply. Notes go to
<profile>/handoffs/<yyyy-mm-dd>/<HHMMSS>-<session>-c<n>.md, where n counts this
session's compressions. post_compaction fires after the rewrite commits and fills in
messages_removed / tokens_before / tokens_after; a note whose compression was aborted
keeps those blank.

Every compression still saves a note, but only from the HANDOFF_AFTER-th
(2nd) compaction on does the "Long tasks and handoff" rule in the project's
agents.md apply: finish the current atomic step, append the agents.md template to
the end of that note, tell the user to /new, stop. transform_tool_result appends that
instruction to tool results (pre_llm_call does it on a new user turn); pre_tool_call
allows GRACE_TOOL_CALLS more calls for the atomic step plus any call touching the
handoffs folder, then blocks the rest. Once the note has a "# Handoff" heading, every
tool is blocked. Failures are logged, never raised (and never block).

Auto-handoff (gateway only): pre_gateway_dispatch remembers the runner and each chat's
source. When the turn that wrote the handoff ends (post_llm_call), the chat is reset
through the gateway's own /new path once it is idle, and the new session is told to read
that note and continue. Under Hermes' session authority a plugin may only continue a chat,
not open one, so when that injection is refused the user is asked to send any message and
that first turn of the new session gets the continue instruction (pre_llm_call). Until the
first inbound message after a gateway start (runner not armed), or with /autohandoff off for
that chat, the user is told to /new instead.

Auto-handoff (classic CLI REPL): the same turn-end queues "/new" and then the continue
instruction on the REPL's input queue, so the REPL starts a new session and continues in it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# plugins/handoff-notes/__init__.py -> the profile folder that owns this plugin
PROFILE_DIR = Path(__file__).resolve().parents[2]
HANDOFF_DIR = PROFILE_DIR / "handoffs"
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_lock = threading.Lock()
# session id -> newest note still waiting for its post_compaction numbers
_pending: dict[str, Path] = {}
_FILLED_FIELDS = ("messages_removed", "tokens_before", "tokens_after", "trigger")
# session id -> newest handoff note; also holds sessions a rotating compaction moved to
_finished: dict[str, Path] = {}
# session id -> tool calls used since compaction (not counting handoff-note ones)
_grace_used: dict[str, int] = {}

# The project's handoff rule and template (project = config.yaml terminal.cwd).
AGENTS_MD = Path(r"C:\Users\chiho\ggen\agents.md")
GRACE_TOOL_CALLS = 6
HANDOFF_AFTER = 2  # ponytail: compaction 1 saves a note only; handoff stops from compaction 2 on
USER_MESSAGE = "Context compacted — handoff written. Start /new and say: continue from {note}"
AUTO_MESSAGE = "Context compacted — handoff written to {note}. Moving to a new session."
RESUME_MESSAGE = ("New session started from handoff {note}. Hermes does not let plugins open a session "
                  "on their own, so send any message (e.g. \"go\") and I will continue from it.")
_CONTINUE = (
    "[handoff-notes] The previous session of this chat handed off to this one. Read the whole "
    "handoff note {note} (it is this chat's latest handoff) and continue the task from its next "
    "action without waiting for user input."
)
_HANDOFF_HEADING = re.compile(r"(?m)^# Handoff\b")

_WRITE_HANDOFF = (
    "[handoff-notes] This conversation now contains two or more context-compaction "
    "summaries, so the "
    "'Long tasks and handoff' rule in agents.md applies. Finish the current atomic step "
    "(never stop mid-edit or mid-run; at most {grace} more tool calls), then call write_file "
    "with path {note} and content = ONLY your handoff, starting with the '# Handoff' heading. "
    "The plugin appends it to the end of that note and keeps the summary already in it, so "
    "do not read the note first. Use this template:\n{template}\n"
    "Then tell the user exactly: \"{user_message}\" and stop. Do not start new work."
)

# Auto-handoff state, in memory only, so a gateway restart never injects anything.
# shortcut: /autohandoff off is forgotten on restart; persist it if that matters.
_ctx = None
_runner = None  # GatewayRunner, armed by the first inbound message after a gateway start
_sources: dict[str, object] = {}  # session key -> latest inbound SessionSource
_rotated: set[str] = set()  # session ids already handed off
_off: set[str] = set()  # session keys with /autohandoff off
_resume: dict[str, Path] = {}  # new session id (minted by our own /new) -> note its first turn continues from


def _auto_key(session_id: str | None) -> str | None:
    # The chat's session key when this session can hand off automatically.
    if _runner is None or not session_id:
        return None
    entries = getattr(getattr(_runner, "session_store", None), "_entries", {}) or {}
    key = next((k for k, e in list(entries.items()) if getattr(e, "session_id", None) == session_id), None)
    return key if key in _sources and key not in _off else None


def _cli():
    # The classic CLI REPL hosting this agent, else None (gateway, TUI/desktop, cron).
    return getattr(getattr(_ctx, "_manager", None), "_cli_ref", None)


def _user_message(session_id: str | None, note: Path) -> str:
    return (AUTO_MESSAGE if _cli() is not None or _auto_key(session_id) else USER_MESSAGE).format(note=note)
_DONE = (
    "[handoff-notes] The handoff is written to {note} and this session is finished. Do not "
    "call any tools. Reply to the user exactly: \"{user_message}\" and stop."
)


def _template() -> str:
    # The "### Handoff template" block of agents.md, up to the next top-level heading.
    try:
        text = AGENTS_MD.read_text(encoding="utf-8")
        block = re.split(r"(?m)^### Handoff template[ \t]*$", text, 1)[1]
        return re.split(r"(?m)^# (?!Handoff)", block, 1)[0].strip()
    except Exception:
        return f"(see the Handoff template in {AGENTS_MD})"


def _handoff_written(note: Path) -> bool:
    # The note itself starts with frontmatter + summary; the bot's handoff adds the heading.
    try:
        return bool(_HANDOFF_HEADING.search(note.read_text(encoding="utf-8")))
    except OSError:
        return False


def _instruction(session_id: str | None) -> str | None:
    note = _handoff_note(session_id)
    if note is None:
        return None
    user_message = _user_message(session_id, note)
    if _handoff_written(note):
        return _DONE.format(note=note, user_message=user_message)
    left = max(0, GRACE_TOOL_CALLS - _grace_used.get(_safe(session_id), 0))
    return _WRITE_HANDOFF.format(grace=left, note=note, template=_template(), user_message=user_message)


def _safe(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)


def _handoff_note(session_id: str | None) -> Path | None:
    # Disk fallback covers notes written before a gateway restart.
    if not session_id or not _active_profile_is_ours():
        return None
    session = _safe(session_id)
    if session not in _finished:
        notes = sorted(HANDOFF_DIR.glob(f"*/*-{session}-c*.md"))
        if len(notes) < HANDOFF_AFTER:
            return None
        _finished[session] = notes[-1]
    return _finished[session]


def _active_profile_is_ours() -> bool:
    # One gateway process can serve several profiles; only archive our own.
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve() == PROFILE_DIR
    except Exception:
        return True


def _next_index(session: str) -> int:
    # Count across all day folders so numbering survives restarts and midnight.
    return 1 + sum(1 for _ in HANDOFF_DIR.glob(f"*/*-{session}-c*.md"))


def on_post_auxiliary_call(**kw) -> None:
    try:
        if kw.get("aux_task") != "compression" or kw.get("error"):
            return
        if not _active_profile_is_ours():
            return
        message = ((kw.get("response") or {}).get("assistant_message") or {})
        content = message.get("content")
        if not isinstance(content, str):
            return
        # The summarizer may quote an earlier handoff; demote it so it never counts as this note's handoff.
        summary = _HANDOFF_HEADING.sub("## Handoff", _THINK_RE.sub("", content).strip())
        if not summary:
            return

        session = _safe(kw.get("session_id") or "no-session")
        usage = kw.get("usage") or {}
        now = datetime.now()
        with _lock:
            day_dir = HANDOFF_DIR / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            n = _next_index(session)
            path = day_dir / f"{now.strftime('%H%M%S')}-{session}-c{n}.md"
            header = "\n".join([
                "---",
                f"session: {kw.get('session_id') or ''}",
                f"platform: {kw.get('platform') or ''}",
                f"compression: {n}",
                f"time: {now.isoformat(timespec='seconds')}",
                f"model: {kw.get('response_model') or kw.get('model') or ''}",
                # Hermes normalizes aux usage to input_tokens/output_tokens (+ prompt_tokens alias).
                f"summarizer_prompt_tokens: {usage.get('prompt_tokens', '')}",
                f"summary_tokens: {usage.get('output_tokens', '')}",
                f"finish_reason: {kw.get('finish_reason') or ''}",
                *(f"{name}: " for name in _FILLED_FIELDS),
                "---",
                "",
            ])
            path.write_text(header + summary + "\n", encoding="utf-8")
            _pending[session] = path
            if n >= HANDOFF_AFTER:
                _finished[session] = path
        logger.info("handoff-notes: saved %s", path)
    except Exception:
        logger.warning("handoff-notes: could not save handoff note", exc_info=True)


def on_post_compaction(**kw) -> None:
    try:
        if kw.get("kind") != "summarize":
            return
        # A rotating commit reports the new session id and the one the summary ran under.
        ids = [kw.get("old_session_id"), kw.get("session_id")]
        values = {
            "messages_removed": kw.get("messages_removed"),
            "tokens_before": kw.get("before_tokens"),
            "tokens_after": kw.get("after_tokens"),
            "trigger": kw.get("reason"),
        }
        with _lock:
            path = None
            for sid in ids:
                if sid:
                    path = _pending.pop(_safe(sid), None) or path
            if path is None or not path.exists():
                return
            if kw.get("session_id") and kw.get("old_session_id"):
                # Carrying the stop to a rotated session: only if the old one had
                # already reached HANDOFF_AFTER compressions.
                old = _safe(kw["old_session_id"])
                if len(list(HANDOFF_DIR.glob(f"*/*-{old}-c*.md"))) >= HANDOFF_AFTER:
                    _finished[_safe(kw["session_id"])] = path
            head, sep, body = path.read_text(encoding="utf-8").partition("\n---\n")
            for name, value in values.items():
                head = re.sub(rf"(?m)^{name}: *$", f"{name}: {'' if value is None else value}", head)
            path.write_text(head + sep + body, encoding="utf-8")
    except Exception:
        logger.warning("handoff-notes: could not record compaction stats", exc_info=True)


def on_pre_tool_call(**kw):
    try:
        session_id = kw.get("session_id")
        note = _handoff_note(session_id)
        if note is None:
            return None
        args = kw.get("args")
        if _handoff_written(note):
            reason = "handoff written"
        elif kw.get("tool_name") == "write_file" and isinstance(args, dict) and _is_note(args.get("path"), note):
            return _append_handoff(session_id, note, args.get("content"))
        elif "handoffs" in str(args or ""):
            return None  # reading/writing the handoff note itself is always allowed
        else:
            used = _grace_used.get(_safe(session_id), 0)
            if used < GRACE_TOOL_CALLS:
                _grace_used[_safe(session_id)] = used + 1
                return None
            reason = "grace calls used up"
        logger.info("handoff-notes: blocked %s in session %s (%s)", kw.get("tool_name"), session_id, reason)
        return {"action": "block", "message": _instruction(session_id)}
    except Exception:
        logger.warning("handoff-notes: pre_tool_call check failed", exc_info=True)
    return None


def _is_note(path, note: Path) -> bool:
    try:
        return bool(path) and Path(path).resolve() == note.resolve()
    except (OSError, ValueError):
        return False


def _append_handoff(session_id: str, note: Path, content) -> dict:
    # A whole-file write_file would clobber the summary (and Hermes' stale-write guard refuses
    # it unless the note was read in full), so the plugin appends the handoff itself.
    match = _HANDOFF_HEADING.search(content) if isinstance(content, str) else None
    if match is None:
        return {"action": "block", "message": (
            f"[handoff-notes] Nothing appended to {note}: the content must contain your handoff "
            "starting with a '# Handoff' heading. Call write_file again with only the handoff.")}
    with _lock:
        existing = note.read_text(encoding="utf-8")
        with note.open("a", encoding="utf-8") as f:
            f.write(("" if existing.endswith("\n") else "\n") + "\n" + content[match.start():].rstrip() + "\n")
    logger.info("handoff-notes: appended handoff to %s", note)
    return {"action": "block", "message": f"[handoff-notes] Appended your handoff to {note}. " + _instruction(session_id)}


def on_transform_tool_result(**kw):
    try:
        text = _instruction(kw.get("session_id"))
        if text is not None and isinstance(kw.get("result"), str):
            return f"{kw['result']}\n\n{text}"
    except Exception:
        logger.warning("handoff-notes: transform_tool_result failed", exc_info=True)
    return None


def on_pre_llm_call(**kw):
    try:
        note = _resume.pop(kw.get("session_id"), None)
        if note is not None:
            return {"context": _CONTINUE.format(note=note)}
        text = _instruction(kw.get("session_id"))
        if text is not None:
            return {"context": text}
    except Exception:
        logger.warning("handoff-notes: pre_llm_call check failed", exc_info=True)
    return None


def on_pre_gateway_dispatch(event=None, gateway=None, **kw):
    global _runner
    try:
        if gateway is not None and event is not None and _active_profile_is_ours():
            _runner = gateway
            _sources[gateway._session_key_for_source(event.source)] = event.source
    except Exception:
        logger.warning("handoff-notes: could not arm auto-handoff", exc_info=True)
    return None


def on_post_llm_call(session_id=None, **kw) -> None:
    # Fires once per turn after the tool loop: the turn that wrote the handoff is ending.
    try:
        note = _handoff_note(session_id)
        if note is None or session_id in _rotated or not _handoff_written(note):
            return
        cli = _cli()
        if cli is not None:
            _rotated.add(session_id)
            # Not inject_message: mid-turn it targets the interrupt queue, i.e. this ending turn.
            # The REPL drains _pending_input after the turn: /new first, then the continue text.
            cli._pending_input.put("/new")
            cli._pending_input.put(_CONTINUE.format(note=note))
            logger.info("handoff-notes: CLI session %s handing off via %s", session_id, note)
            return
        key = _auto_key(session_id)
        if key is None:
            return  # fallback: the user was told to /new
        _rotated.add(session_id)
        asyncio.run_coroutine_threadsafe(_rotate(key, session_id, note), _runner._gateway_loop)
    except Exception:
        logger.warning("handoff-notes: could not schedule auto-handoff", exc_info=True)


async def _rotate(key: str, old_session_id: str, note: Path) -> None:
    try:
        from gateway.platforms.event import MessageEvent, MessageType
        # Never reset mid-turn: wait (up to 10 min) for the turn and its reply to finish.
        for _ in range(600):
            if key not in _runner._running_agents:
                break
            await asyncio.sleep(1)
        else:
            logger.warning("handoff-notes: %s still busy, auto-handoff skipped", key)
            return
        entry = _runner.session_store._entries.get(key)
        if key in _off or entry is None or entry.session_id != old_session_id:
            return  # turned off, or the user already started a new session (manual /new wins)
        await _runner._handle_reset_command(MessageEvent(
            text="/new", message_type=MessageType.TEXT, source=_sources[key], internal=True))
        from gateway.session_authorities import active_authority
        # Under a session authority inject_message still returns True, but dispatch then refuses it
        # (the fresh session has no human message yet), so skip straight to the fallback there.
        if active_authority(_runner) is None and _ctx.inject_message(_CONTINUE.format(note=note), session_key=key):
            logger.info("handoff-notes: %s handed off from %s via %s", key, old_session_id, note)
            return
        # The fresh session's first human message resumes it instead.
        new = _runner.session_store._entries.get(key)
        if new is not None:
            _resume[new.session_id] = note
        source = _sources[key]
        await _runner._delivery_adapter_for(source).send(source.chat_id, RESUME_MESSAGE.format(note=note.name))
        logger.info("handoff-notes: %s reset from %s; waiting for a message to resume via %s",
                    key, old_session_id, note)
    except Exception:
        logger.warning("handoff-notes: auto-handoff failed for %s", key, exc_info=True)


def _autohandoff_command(raw_args: str = "", session_key: str | None = None) -> str:
    arg = (raw_args or "").strip().lower()
    if session_key and arg == "off":
        _off.add(session_key)
    elif session_key and arg == "on":
        _off.discard(session_key)
    state = "off" if session_key in _off else "on"
    armed = "" if _runner is not None else " (arms on the next message after a gateway start)"
    return f"Auto-handoff is {state} for this chat{armed}."


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_command("autohandoff", _autohandoff_command,
                         description="Auto-handoff to a new session: on|off|status", args_hint="on|off|status")
    ctx.register_hook("post_auxiliary_call", on_post_auxiliary_call)
    ctx.register_hook("post_compaction", on_post_compaction)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
