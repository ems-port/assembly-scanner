# Fusion MCP troubleshooting

This guide summarizes the Fusion MCP failure observed during Assembly Scanner development
and the recovery path implemented in version 0.2.0.

## Connection details

The client defaults to:

```text
http://127.0.0.1:27182/mcp
```

This is a loopback HTTP endpoint provided by Fusion's MCP integration. The client sends MCP
JSON-RPC requests, establishes a session with `initialize`, and sends that session ID on
subsequent calls. When a request fails, it discards the old session and attempts to create a
new one once.

The endpoint can be overridden for diagnostics with `ASCAN_FUSION_URL`; normal use should
keep it on loopback.

## Observed recursion failure

The app displayed `maximum recursion depth exceeded`, sometimes alongside a disconnected
status. Restarting the MCP integration alone did not always restore the app's old session.
The MCP listener and basic protocol handshake were reachable during diagnosis. The
structured document/Electronics reads succeeded. A no-op Fusion script succeeded, and a
read-only script exception returned a normal error response in a separate diagnostic run.
That evidence did not isolate one universal Fusion cause; it showed that the failure was
not simply an occupied port or an always-broken MCP endpoint.

The recursion trace pointed into the MCP/Fusion output-writing path. To reduce exposure to
that path, Assembly Scanner now reads board information through structured read tools and
suppresses script stdout for actions. This is a defensive workaround based on observed
behavior, not a claim that every Fusion recursion failure has the same root cause.

## Recovery steps

1. In Assembly Scanner, click **Rescan boards** once and read the Fusion status line.
2. If it remains disconnected, confirm Fusion is open and its MCP integration is enabled.
3. Run the read-only status check from the project directory:

   ```bash
   .venv/bin/python -m app.fusion_client status
   ```

4. If the CLI connects but the app does not, quit and relaunch Assembly Scanner. The app
   creates a fresh MCP session at startup.
5. If both fail, restart the Fusion MCP integration/server and retry the status command.
   There is no separate MCP reset command in this integration; reconnecting creates a new
   client session.
6. If Fusion itself must be restarted, save the project first. Assembly Scanner cannot
   recover a Fusion process that has stopped responding.

Avoid repeatedly pressing **Rescan boards** while the same recursion error is present. The
app backs off automatic checks after three identical recursion errors; a deliberate rescan
clears that backoff and starts one new check cycle.

## Ports 9000 and 9229

Fusion's debug endpoints commonly use port 9000, and a TypeScript/Node debugger may use
9229. They are separate from the MCP listener on 27182, so they do not directly collide
with the Assembly Scanner MCP connection. A port conflict requires another process to bind
the same port. Debug ports can affect debugger attachment if already in use; they are not
the MCP reset or connection port.

## What to include in a bug report

- macOS version and Fusion version/build.
- Assembly Scanner version from the app bundle (`0.2.0` for this release).
- The complete output of `python -m app.fusion_client status`.
- The exact error line and whether the MCP integration was restarted.
- Whether Fusion remained responsive and whether the board document was active.

Before sharing logs, remove BOM paths, project names, part lists, and account information.
