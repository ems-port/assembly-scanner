# Changelog

## 0.2.0 — 2026-10-14

Public preview focused on reliability and workshop usability.

### Added

- Pause and resume camera capture, barcode decoding, and Fusion MCP polling. The app also
  pauses when sent to the background and resumes when brought forward.
- Camera image inversion, copyable part results, and a `sum: N` count for results with more
  than three designators.
- Reconnect-once MCP session recovery, retry backoff after repeated recursion errors, and a
  manual **Rescan boards** recovery action.
- Structured Fusion document/Electronics reads for board context and silent script output
  for selection actions.
- Public architecture, installation, and Fusion MCP troubleshooting documentation.

### Fixed

- A false “not in schematic” warning could appear even when Fusion visibly cross-probed the
  selected parts. The warning came from comparing the active board with a mismatched
  schematic-part read. Reporting now uses the successful selection action and board
  placements.
- Rescan and reconnect behavior after repeated Fusion MCP errors.

### Diagnostic findings

- On 2026-10-14, a fresh read-only CLI status request connected to Fusion MCP and returned
  the active Electronics document and its element names.
- The MCP handshake, structured document/Electronics reads, a no-op script, and a script
  error response all worked in separate diagnostics. The recursion error therefore did not
  reproduce on every request.
- The observed trace pointed into the Fusion MCP output-writing path. The exact trigger was
  not conclusively isolated; structured reads, silent action scripts, session renewal, and
  bounded retry behavior are defensive mitigations.
- Ports 9000 and 9229 are separate from the MCP endpoint on 27182 and do not directly
  conflict with it.
- No independent MCP reset command was found. Reconnecting establishes a new MCP client
  session; restarting Fusion should be a last resort after saving the project.

### Build

- macOS app bundle version: `0.2.0` (build `2`).
- Ad-hoc signed for local camera-permission identity; not Developer ID signed or notarized.
