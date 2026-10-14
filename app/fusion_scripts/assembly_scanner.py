"""Runs INSIDE Fusion via the stock MCP `fusion_mcp_execute` (featureType "script", readOnly true).

Sent as source text by app/fusion_client.py, which replaces the __ARGS__ token with a
Python string literal holding JSON:
    {"op": "status"|"list"|"designators"|"highlight"|"clear",
     "target": <board key or null>, "clear_keys": [<board keys>],
     "designators": [...], "zoom": bool, "activate": bool}

Highlight mechanism: adsk.electron.Board has no selection API,
but adsk.electron.Schematic.selectObjects(parts, zoom) selects the parts in the schematic,
and Fusion cross-probes that selection onto the board: the selected elements are drawn
with bright pads, a blue outline and a white bounding box in the board editor.
Schematic.clearSelection() removes it. Each design keeps its own selection, and a
selection made on a design in a background tab shows when that tab is brought forward
(verified with multiple Electronics documents open).

Everything here runs in a read-only script: no design change and no undo entry. It never
calls executeTextCommand, save or close. Document.activate() (bringing the target board tab
forward) is allowed in read-only mode and leaves isModified false.

Board key: the board document's dataFile lineage URN, which stays the same across saves.
When two open documents share a lineage (two versions of one file), "@v<N>" is appended.
An unsaved board has the key "new:<creationId>".

Target rule: with a target key, act on exactly that board, or raise an error if it
is not open; there is no fallback. Without a target, act only when exactly one board is open.

Output: one line, `ASCAN_JSON:` followed by a JSON object. Errors are raised, not caught
(the MCP returns them as success:false).
"""
import json

import adsk.core
import adsk.electron

ARGS = json.loads(__ARGS__)
MARK = "ASCAN_JSON:"


def _emit(obj):
    # Fusion's MCP script-output writer can recurse in long-lived sessions. The desktop
    # app uses the separate Electronics read tools for data and runs actions silently.
    # Keep stdout output for manual diagnostics only.
    if not ARGS.get("silent_output", False):
        print(MARK + json.dumps(obj))


def _boards(app):
    """[(key, BoardDocument, info dict)] for every open board document."""
    raw = []
    for d in app.documents:
        bd = adsk.electron.BoardDocument.cast(d)
        if not bd:
            continue
        df = d.dataFile
        lineage = df.id if df else None
        raw.append((lineage, df.versionNumber if df else None, bd))
    counts = {}
    for lin, _, _ in raw:
        counts[lin] = counts.get(lin, 0) + 1
    active = app.activeDocument
    out = []
    for lin, ver, bd in raw:
        if lin is None:
            key = "new:" + bd.creationId
        elif counts[lin] > 1:
            key = "%s@v%s" % (lin, ver)
        else:
            key = lin
        b = bd.board
        sch = b.linkedSchematic if b else None
        info = {"key": key, "lineage": lin, "version": ver, "name": bd.name,
                "has_board": b is not None, "has_schematic": sch is not None,
                "is_active": bool(active and active.creationId == bd.creationId),
                "is_modified": bd.isModified}
        if b:
            els = b.elements
            info["designators"] = [els.item(i).name for i in range(els.count)]
            info["designator_count"] = els.count
        out.append((key, bd, info))
    return out


def _pick(boards, target):
    if target:
        hit = [x for x in boards if x[0] == target]
        if not hit:  # a lineage key whose doc now has a version suffix, or the reverse
            base = target.split("@v")[0]
            hit = [x for x in boards if x[2]["lineage"] == base]
            if len(hit) != 1:
                hit = []
        if not hit:
            raise RuntimeError("BOARD_NOT_OPEN: the selected board (%s) is no longer open in Fusion." % target)
        return hit[0]
    if not boards:
        raise RuntimeError("NO_BOARD: no Fusion Electronics board is open.")
    if len(boards) > 1:
        raise RuntimeError("AMBIGUOUS_BOARD: %d boards are open (%s); select one."
                           % (len(boards), ", ".join(x[2]["name"] for x in boards)))
    return boards[0]


def run(_context: str):
    app = adsk.core.Application.get()
    op = ARGS.get("op", "status")
    boards = _boards(app)
    ad = app.activeDocument
    if op in ("status", "list"):
        _emit({"op": op, "active_doc": ad.name if ad else None,
               "boards": [x[2] for x in boards]})
        return
    key, bd, info = _pick(boards, ARGS.get("target"))
    out = {"op": op, "board": {k: v for k, v in info.items() if k != "designators"}}
    if op == "activate":
        out["activated"] = bool(bd.activate()) if not info["is_active"] else False
        _emit(out)
        return
    if op == "designators":
        out["board_designators"] = info.get("designators", [])
        _emit(out)
        return
    sch = bd.board.linkedSchematic
    if sch is None:
        raise RuntimeError("NO_SCHEMATIC: board %s has no linked schematic; cross-probe highlight is unavailable." % info["name"])
    # Clear the previous highlight, which may be on another board (each design keeps its own selection).
    for ck in ARGS.get("clear_keys", []):
        for k2, bd2, _ in boards:
            if k2 == ck and k2 != key and bd2.board and bd2.board.linkedSchematic:
                bd2.board.linkedSchematic.clearSelection()
    sch.clearSelection()
    if op == "clear":
        out["highlighted"] = []
        _emit(out)
        return
    if op != "highlight":
        raise ValueError("unknown op %r" % op)
    on_board = {n.upper(): n for n in info.get("designators", [])}
    want = []
    for d in ARGS.get("designators", []):
        u = str(d).strip().upper()
        if u and u not in want:
            want.append(u)
    parts = {p.name.upper(): p for p in sch.parts}
    not_on_board = [d for d in want if d not in on_board]
    no_part = [d for d in want if d in on_board and d not in parts]
    sel = [parts[d] for d in want if d in on_board and d in parts]
    ok = sch.selectObjects(sel, bool(ARGS.get("zoom", False))) if sel else True
    activated = False
    if ARGS.get("activate", True) and not info["is_active"]:
        activated = bool(bd.activate())
    out.update(selected_ok=bool(ok), activated=activated,
               highlighted=[on_board[d] for d in want if d in on_board and d in parts],
               not_on_board=not_on_board, no_schematic_part=no_part)
    _emit(out)
