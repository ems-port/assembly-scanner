# Architecture

Assembly Scanner has four stages:

```text
camera frame → Code 128 decode → BOM resolution → Fusion Electronics selection
```

## Runtime components

| Component | Responsibility |
|---|---|
| `app/camera.py` | Opens the selected camera and publishes the latest frame from a capture thread. |
| `app/scanner.py` | Decodes Code 128 symbols from frames and reports scanner health. |
| `app/bom.py` | Reads spreadsheet/CSV rows, recognizes headers, parses designators, and records import warnings. |
| `app/resolver.py` | Matches decoded barcodes against the imported part-number index. The BOM supplies placement designators. |
| `app/state.py` | Debounces repeated camera reads so a label fires once while held in view. |
| `app/ui.py`, `app/hud.py` | Presents the camera, controls, status, and copyable part result. `hud.py` formats display rows without Qt. |
| `app/fusion_client.py` | Owns the MCP HTTP session, board targeting, retry behavior, and Fusion actions. |
| `app/fusion_scripts/assembly_scanner.py` | Runs a small read-only Fusion script for actions such as selecting or clearing schematic objects. |
| `launcher/` | Installs a macOS app bundle with a stable camera permission identity. |

## Fusion selection flow

The Fusion MCP server does not expose a dedicated component-highlight action. Assembly
Scanner calls `Schematic.selectObjects()` in a read-only Fusion script; Fusion then
cross-probes the selected schematic objects onto the board. Clearing the selection uses
`Schematic.clearSelection()`. The action does not save or modify the design.

Board and document data is queried through Fusion's structured document and Electronics
read tools. Script stdout is intentionally suppressed for actions because recursive output
errors were observed in the Fusion MCP integration. The client treats a successful silent
action response as an acknowledgement, then uses structured reads for board context.

## Targeting and recovery

- **Auto** mode compares BOM designators with open board data and selects only a sufficiently
  clear match. Ambiguous boards do not receive a selection.
- **Manual** mode pins the chosen board for the current app session. A closed target fails
  visibly; the client does not silently redirect to another board.
- The HTTP client maintains an MCP session, discards it after a transport/protocol failure,
  and retries once with a new session.
- The UI polls Fusion while active. Pause and app backgrounding stop new camera and MCP
  work. Repeated identical recursion errors open a retry backoff until the user requests a
  board rescan.

## Data and privacy

The BOM is selected locally by the user. Camera frames, decoded barcodes, and board data are
processed locally. The only network request made by the Fusion integration is to the local
Fusion MCP endpoint, by default `http://127.0.0.1:27182/mcp`.

## Logs

- Settings: `~/Library/Application Support/AssemblyScanner/config.json`
- App log: `~/Library/Logs/AssemblyScanner.log`

Logs may contain local file names and error text. Review and redact them before sharing.
