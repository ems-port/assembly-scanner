# Roadmap

This is a direction for future work, not a promise or current feature list. Items here are
not implemented unless stated otherwise.

## BOM assembly history

Record assembly progress alongside a loaded BOM workbook:

- Store a **last scanned** timestamp when a part-number label is recognized.
- Support a separate **placed at** timestamp only when placement is explicitly confirmed;
  a barcode scan by itself cannot confirm that a component was physically installed.
- Preserve the original BOM rows and add an **Additions / needs review** section at the
  bottom for scans that have no matching BOM line, no designators, or designators absent
  from the selected Fusion board.
- Keep unknown or incomplete designators as reviewable text; never invent placements or
  silently attach an unmatched scan to an existing BOM line.
- Decide the save policy (updated copy versus editing the loaded workbook), duplicate-scan
  handling, and timestamp format before implementation.

The feature should remain optional and should not interrupt barcode scanning or Fusion
highlighting.
