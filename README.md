# handoff-notes

Hermes plugin (v0.8.3, standalone) that keeps a long-running chat going across context compressions.

- **Every compression:** the summary Hermes writes is saved as a Markdown note.
- **From the Nth compression on** (N = `handoff_after`, default 2): the agent writes a handoff and the chat carries forward from it, so summaries don't keep piling on top of each other.

## What it does

1. **Save a note per compression.** The summarizer reply is saved to
   `<profile>/handoffs/<yyyy-mm-dd>/<HHMMSS>-<session>-c<n>.md`. Here `n` counts this session's compressions.
   When the compression commits, the note also gets `messages_removed`, `tokens_before`, `tokens_after` and `trigger`.
   If the compression was aborted, those fields stay blank.
2. **Ask for a handoff at compression N.** The agent is told to:
   - finish the current atomic step (at most 6 more tool calls);
   - append a `# Handoff` section to the note;
   - tell the user, then stop.

   While this happens, other tool calls are blocked. Calls that touch the handoffs folder are always allowed.
3. **Carry the chat forward** (gateway only), once the turn ends and the chat is idle:
   - **Local routes, and any chat under a session authority:** the chat continues **in the same session**.
     The history is soft-archived, the handoff is injected inline, and the compression count restarts.
     If that reset fails, the plugin falls back to `/compress here 1`.
   - **Messaging chats on a gateway without a session authority:** the plugin runs the gateway's `/new` and tells the new session to continue from the note.
     If that injection fails, the user is asked to send any message, and that first turn gets the instruction.
   - **No gateway in the process, or `/autohandoff off`:** the user is told to start `/new` themselves.

The plugin logs failures and never raises them, so it never blocks or aborts a compression.

## Configuration

Set this in the profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - handoff-notes
  entries:
    handoff-notes:
      allow_gateway_injection: true   # required for auto-continue
      settings:
        handoff_after: 2              # compressions before the handoff carries the chat forward
```

| Key | Default | Meaning |
|---|---|---|
| `allow_gateway_injection` | — | Lets the plugin inject the "continue" message. Without it the user has to `/new` by hand. |
| `settings.handoff_after` | `2` | Compression count that triggers the handoff. Must be an integer ≥ 1; any other value logs a warning and falls back to 2. |

`handoff_after` must sit under `settings:`. That is where Hermes's `ctx.get_config` looks, and Hermes ignores misplaced keys without warning.
The value is read when the plugin loads, so restart the gateway after changing it.
The log then shows `handoff-notes: handoff after N compressions`.

## Command

`/autohandoff on|off|status` turns automatic carry-forward on or off for the current chat.
The setting is kept in memory only, so a gateway restart forgets `off`.

## Handoff template

The agent fills in the `### Handoff template` section of `agents.md` in the session's working directory.
If that section doesn't exist, it uses `handoff_template.md` from this folder, which you can edit.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | The plugin |
| `plugin.yaml` | Manifest and hook list |
| `handoff_template.md` | Default handoff template |
| `selftest_handoff_after.py` | Checks that `handoff_after` is read from config and that bad values are rejected |
| `selftest_reset.py` | Checks the in-place reset against a temp SessionDB |

## Testing

Run the self-tests with Hermes's own interpreter, the one `%LOCALAPPDATA%\hermes\bin\hermes.cmd` launches:

```powershell
& "$env:LOCALAPPDATA\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe" selftest_handoff_after.py
```

Both tests import `hermes_bootstrap`. If a Hermes update is pending, that import can install dependencies first.

Debug traces are logged at INFO with the prefix `handoff-notes[dbg]` in `gateway.log` / `agent.log`.
`llm\scripts\handoff-summary-report.ps1` builds a per-session table of summary sizes from the notes.
