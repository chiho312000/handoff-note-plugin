"""Save each context-compression summary as a handoff note.

The summarizer runs as an auxiliary LLM call with aux_task="compression", and
post_auxiliary_call carries its reply. Notes go to
<profile>/handoffs/<yyyy-mm-dd>/<HHMMSS>-<session>-c<n>.md, where n counts this
session's compressions. post_compaction fires after the rewrite commits and fills in
messages_removed / tokens_before / tokens_after; a note whose compression was aborted
keeps those blank.

Every compression still saves a note, but only from the HANDOFF_AFTER-th
(2nd) compaction since the last in-place continue is the agent told to hand off: finish the
current atomic step, append a
handoff (the "### Handoff template" of agents.md in the session's cwd, else handoff_template.md) to
the end of that note, tell the user, stop. Once a note holds a handoff it stays the session's
note even if a later compression saves another. transform_tool_result appends that
instruction to tool results (pre_llm_call does it on a new user turn); pre_tool_call
allows GRACE_TOOL_CALLS more calls for the atomic step plus any call touching the
handoffs folder, then blocks the rest. Once the note has a "# Handoff" heading, every
tool is blocked. Failures are logged, never raised (and never block).

Auto-handoff (gateway): the runner is the one pre_gateway_dispatch saw, else the host the gateway
publishes to plugin managers (Local desktop/TUI turns never fire pre_gateway_dispatch); a chat's
source is the latest inbound one, else its session entry's restored origin. When the turn that
wrote the handoff ends (post_llm_call), once the chat is idle, the chat continues in the same
session: the note is marked continued, which restarts the compression count and lifts the tool
block; the gateway's own "/compress here 1" then summarizes all but the last turn (the nearest
thing to /clear that keeps the session), and the continue instruction is injected. That covers Local routes (the classic
CLI, TUI and desktop app are gateway viewers of one session and never follow a gateway-side
reset) and every chat under a session authority (a plugin may continue a chat but not open the
fresh session a reset makes). Only a gateway without a session authority resets messaging chats
through its own /new path and tells the new session to continue; if that injection fails, the
user is asked to send any message and that first turn gets the instruction (pre_llm_call).
With no gateway in the process, or with /autohandoff off for that chat, the user is told to /new.
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

GRACE_TOOL_CALLS = 6
HANDOFF_AFTER = 2  # ponytail: compaction 1 saves a note only; handoff stops from compaction 2 on
USER_MESSAGE = "Context compacted — handoff written. Start /new and say: continue from {note}"
AUTO_MESSAGE = "Context compacted — handoff written to {note}. Moving to a new session."
IN_PLACE_MESSAGE = ("Context compacted — handoff written to {note}. Clearing the context and continuing "
                    "from it in this session.")
RESUME_MESSAGE = ("New session started from handoff {note}. Hermes does not let plugins open a session "
                  "on their own, so send any message (e.g. \"go\") and I will continue from it.")
DEFAULT_TEMPLATE = Path(__file__).with_name("handoff_template.md")  # generic template, editable
_CONTINUE = (
    "[handoff-notes] The previous session of this chat handed off to this one. Read the whole "
    "handoff note {note} (it is this chat's latest handoff) and continue the task from its next "
    "action without waiting for user input."
)
_CONTINUE_IN_PLACE = (
    "[handoff-notes] Your handoff is saved in {note}. The context was compacted, so read that "
    "note and continue the task from its next step in this session, without waiting for user input."
)
_HANDOFF_HEADING = re.compile(r"(?m)^# Handoff\b")
# Appended to a note whose handoff was continued in place: it closes the note's window.
_CONTINUED_MARK = "<!-- handoff-notes: continued in this session -->"

_WRITE_HANDOFF = (
    "[handoff-notes] This conversation now contains two or more context-compaction "
    "summaries, so it is time to write a handoff. Finish the current atomic step "
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


def _gateway():
    # The gateway runner: armed by pre_gateway_dispatch, else the host the gateway publishes to every
    # plugin manager (Local desktop/TUI turns never fire pre_gateway_dispatch).
    if _runner is not None:
        return _runner
    host = getattr(getattr(_ctx, "_manager", None), "_gateway_message_injector", None)
    return host[0] if host else None


def _auto_key(session_id: str | None) -> str | None:
    # The chat's session key when this session can hand off automatically.
    runner = _gateway()
    if runner is None or not session_id:
        return None
    entries = getattr(getattr(runner, "session_store", None), "_entries", {}) or {}
    key = next((k for k, e in list(entries.items()) if getattr(e, "session_id", None) == session_id), None)
    if key is None or key in _off:
        return None
    if key not in _sources:  # never dispatched through pre_gateway_dispatch: rebuild its origin
        try:
            source = runner._restored_source(entries[key])
        except Exception:
            source = None
        if source is None:
            return None
        _sources[key] = source
    return key


def _is_local(key: str) -> bool:
    # A Local route: the classic CLI, TUI or desktop app viewing a gateway session.
    return str(getattr(getattr(_sources.get(key), "platform", None), "value", "")) == "local"


def _in_place(key: str) -> bool:
    # Continue in the same session: Local viewers never follow a gateway reset, and under a session
    # authority a plugin may continue a chat but not open the fresh session a reset makes.
    if _is_local(key):
        return True
    try:
        from gateway.session_authorities import active_authority
        return active_authority(_gateway()) is not None
    except Exception:
        return False


def _user_message(session_id: str | None, note: Path) -> str:
    key = _auto_key(session_id)
    message = USER_MESSAGE if key is None else IN_PLACE_MESSAGE if _in_place(key) else AUTO_MESSAGE
    return message.format(note=note)
_DONE = (
    "[handoff-notes] The handoff is written to {note} and this session is finished. Do not "
    "call any tools. Reply to the user exactly: \"{user_message}\" and stop."
)


def _template() -> str:
    # The "### Handoff template" block of the session project's agents.md, up to the next
    # top-level heading; projects without one get the generic template in the plugin folder.
    try:
        text = (Path(_ctx.session_cwd()) / "agents.md").read_text(encoding="utf-8")
        block = re.split(r"(?m)^### Handoff template[ \t]*$", text, 1)[1]
        return re.split(r"(?m)^# (?!Handoff)", block, 1)[0].strip()
    except Exception:
        pass
    try:
        return DEFAULT_TEMPLATE.read_text(encoding="utf-8").strip()
    except OSError:
        return "# Handoff — <date/time>\n(goal, done & verified, current state, failed attempts, next step)"


def _handoff_written(note: Path) -> bool:
    # The note itself starts with frontmatter + summary; the bot's handoff adds the heading.
    try:
        return bool(_HANDOFF_HEADING.search(note.read_text(encoding="utf-8")))
    except OSError:
        return False


def _continued(note: Path) -> bool:
    try:
        return _CONTINUED_MARK in note.read_text(encoding="utf-8")
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
        # Sort by compression number (c10 after c9); only notes after the last in-place continue count.
        notes = sorted(HANDOFF_DIR.glob(f"*/*-{session}-c*.md"),
                       key=lambda n: int(re.search(r"-c(\d+)\.md$", n.name).group(1)))
        last = max((i for i, n in enumerate(notes) if _continued(n)), default=-1)
        notes = notes[last + 1:]
        if len(notes) < HANDOFF_AFTER:
            return None
        # A note that already holds the handoff wins over any later compression's note.
        _finished[session] = next((n for n in reversed(notes) if _handoff_written(n)), notes[-1])
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
            prev = _handoff_note(kw.get("session_id"))  # before this note exists on disk
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
            # A compression after the handoff was written must not replace the note that holds it;
            # otherwise the next lookup recounts from disk (notes since the last in-place continue).
            if not (prev and _handoff_written(prev)):
                _finished.pop(session, None)
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
                old_note = _handoff_note(kw["old_session_id"])
                if old_note is not None:
                    _finished[_safe(kw["session_id"])] = old_note if _handoff_written(old_note) else path
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
    # The current note or an older note of the same session: after another compression the bot
    # often still names the note it was told about before (that write would hit Hermes' stale guard).
    def session_of(p: Path) -> str:
        return re.sub(r"-c\d+\.md$", "", p.name).split("-", 1)[-1]
    try:
        p = Path(path).resolve()
    except (OSError, ValueError, TypeError):
        return False
    return (p.suffix == ".md" and p.parent.parent == note.resolve().parent.parent
            and session_of(p) == session_of(note))


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
        key = _auto_key(session_id)
        if key is None:  # fallback: the user was told to /new
            logger.info("handoff-notes: no auto-handoff for %s (gateway=%s, off/unrouted)",
                        session_id, _gateway() is not None)
            return
        _rotated.add(session_id)
        step = _continue_in_place if _in_place(key) else _rotate
        asyncio.run_coroutine_threadsafe(step(key, session_id, note), _gateway()._gateway_loop)
    except Exception:
        logger.warning("handoff-notes: could not schedule auto-handoff", exc_info=True)


async def _idle_and_current(runner, key: str, session_id: str) -> bool:
    # Never act mid-turn: wait (up to 10 min) for the turn and its reply to finish; then only if the
    # chat is still on this session (turned off, or a manual /new by the user, wins).
    for _ in range(600):
        if key not in runner._running_agents:
            break
        await asyncio.sleep(1)
    else:
        logger.warning("handoff-notes: %s still busy, auto-handoff skipped", key)
        return False
    entry = runner.session_store._entries.get(key)
    return key not in _off and entry is not None and entry.session_id == session_id


def _close_window(session_id: str, note: Path | None = None) -> None:
    # Mark `note` (default: the session's newest note) continued: notes up to it no longer count.
    if note is None:
        notes = sorted(HANDOFF_DIR.glob(f"*/*-{_safe(session_id)}-c*.md"),
                       key=lambda n: int(re.search(r"-c(\d+)\.md$", n.name).group(1)))
        note = notes[-1] if notes else None
    with _lock:
        if note is not None and not _continued(note):
            with note.open("a", encoding="utf-8") as f:
                f.write("\n" + _CONTINUED_MARK + "\n")
        _finished.pop(_safe(session_id), None)
        _grace_used.pop(_safe(session_id), None)


async def _continue_in_place(key: str, session_id: str, note: Path) -> None:
    try:
        runner = _gateway()
        if not await _idle_and_current(runner, key, session_id):
            return
        _close_window(session_id, note)  # the count restarts and tools are no longer blocked
        # The closest thing to /clear that keeps the session (/clear and /new open a new one): summarize
        # everything but the last turn. Still the same session, so the continue below stays allowed.
        try:
            from gateway.platforms.event import MessageEvent, MessageType
            result = await runner._handle_compress_command(MessageEvent(
                text="/compress here 1", message_type=MessageType.TEXT, source=_sources[key], internal=True))
            logger.info("handoff-notes: compacted %s before continuing: %s", key, str(result)[:200])
        except Exception:
            logger.warning("handoff-notes: compaction before continuing failed for %s", key, exc_info=True)
        _close_window(session_id)  # that compaction's own note must not count toward the next handoff
        if _ctx.inject_message(_CONTINUE_IN_PLACE.format(note=note), session_key=key):
            logger.info("handoff-notes: %s continues in session %s via %s", key, session_id, note)
        else:
            logger.warning("handoff-notes: continue message for %s was refused; the user can say "
                           "'continue from %s'", key, note)
    except Exception:
        logger.warning("handoff-notes: in-place continue failed for %s", key, exc_info=True)
    finally:
        _rotated.discard(session_id)  # a later handoff of this session may continue again


async def _rotate(key: str, old_session_id: str, note: Path) -> None:
    try:
        from gateway.platforms.event import MessageEvent, MessageType
        runner = _gateway()
        if not await _idle_and_current(runner, key, old_session_id):
            return
        await runner._handle_reset_command(MessageEvent(
            text="/new", message_type=MessageType.TEXT, source=_sources[key], internal=True))
        from gateway.session_authorities import active_authority
        # Under a session authority inject_message still returns True, but dispatch then refuses it
        # (the fresh session has no human message yet), so skip straight to the fallback there.
        if active_authority(runner) is None and _ctx.inject_message(_CONTINUE.format(note=note), session_key=key):
            logger.info("handoff-notes: %s handed off from %s via %s", key, old_session_id, note)
            return
        # The fresh session's first human message resumes it instead.
        new = runner.session_store._entries.get(key)
        if new is not None:
            _resume[new.session_id] = note
        source = _sources[key]
        await runner._delivery_adapter_for(source).send(source.chat_id, RESUME_MESSAGE.format(note=note.name))
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
    armed = "" if _gateway() is not None else " (no gateway in this process)"
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
