"""HUD text. Pure and Qt-free; ui.py only paints these lines.

``hud_lines(resolution, highlight=None, ctx=None) -> list[(text, level)]``
level in ok / warn / error / info.

ctx keys (all optional):
  bom_name, bom_summary     loaded BOM file name and its one-line summary
  board_name, board_mode    target Fusion board and "auto" / "manual"
  board_note                extra board text, e.g. "covers 100% of BOM placements"
  board_warning             why there is no usable board (ambiguous, low coverage...)
  fusion_error              Fusion unreachable / pinned board closed / ...
  camera_ok, camera_error   camera state
  scanner_error             decode loop stalled / no results (red)
  label_seen                a label is in view (>= 2 Code 128 decoded) but no code
                            resolves: the P/N barcode itself was not readable (amber)
  blocked_reason            a MATCH was not highlighted because of this
  pending                   True while the highlight call is in flight

``hud_sections`` returns the same content split for painting: (top, bottom), each a
list of (text, level, size) with size "small" | "mid" | "big".
"""
from __future__ import annotations

from app.models import HighlightResult, Resolution, Status

Line = tuple[str, str]
Item = tuple[str, str, str]


def _project(ctx: dict) -> list[Item]:
    out: list[Item] = []
    bom_name = ctx.get("bom_name")
    if ctx.get("bom_error"):
        out.append((f"BOM  {ctx['bom_error']}", "error", "small"))
    elif bom_name:
        summ = ctx.get("bom_summary")
        out.append((f"BOM  {bom_name}" + (f" · {summ}" if summ else ""), "info", "small"))
    else:
        out.append(("BOM  none loaded: click 'Load BOM…'", "warn", "small"))

    mode = ctx.get("board_mode") or "auto"
    if ctx.get("fusion_error"):
        name = ctx.get("board_name")
        pre = f"BOARD  {name} ({mode}): " if name else "FUSION  "
        out.append((pre + str(ctx["fusion_error"]), "error", "small"))
    elif ctx.get("board_name"):
        note = ctx.get("board_note")
        out.append((f"BOARD  {ctx['board_name']} ({mode})" + (f" · {note}" if note else ""),
                    "ok", "small"))
    else:
        why = ctx.get("board_warning") or "no board selected"
        out.append((f"BOARD  none ({mode}): {why}. Not highlighting.", "warn", "small"))

    if ctx.get("scanner_error"):
        out.append((f"SCANNER  {ctx['scanner_error']}", "error", "small"))
    if ctx.get("camera_ok") is False:
        out.append((f"CAMERA  {ctx.get('camera_error') or 'not available'}", "error", "small"))
    return out


LABEL_SEEN = "LABEL SEEN — part-number barcode not readable: hold still, level, ~25 cm"


def _result(res: Resolution | None, hl: HighlightResult | None, ctx: dict) -> list[Item]:
    out = _last_result(res, hl, ctx)
    if ctx.get("label_seen"):
        seen = (LABEL_SEEN, "warn", "mid")
        return [seen] + out if out else [seen]
    if not out and ctx.get("bom_name"):
        return [("show a Mouser bag label (inside the dashed box, roughly level)", "info", "mid")]
    return out


def _last_result(res: Resolution | None, hl: HighlightResult | None, ctx: dict) -> list[Item]:
    """The last FIRED result. It stays on screen after the bag leaves (like the highlight)."""
    if res is None or res.status == Status.NO_CANDIDATE:
        return []
    pn = res.mouser_pn or ""
    ln = res.lines[0] if res.lines else None
    desc = " · ".join(x for x in ((ln.description if ln else None), (ln.package if ln else None)) if x)

    if res.status == Status.NOT_IN_BOM:
        return [(f"✕ NOT IN CURRENT BOM  {pn}", "error", "big"),
                ("highlight unchanged", "info", "mid")]
    if res.status == Status.MATCH_NO_PLACEMENT:
        out = [(f"NO PLACEMENT FOUND  {pn}", "warn", "big")]
        if desc:
            out.append((desc, "info", "mid"))
        out.append(("in BOM, no placement (extra item); highlight unchanged", "info", "mid"))
        return out

    # MATCH
    desigs = " ".join(res.designators)
    if len(res.designators) > 3:
        desigs += f"  sum: {len(res.designators)}"
    level = "ok"
    detail: list[Item] = []
    if ctx.get("blocked_reason"):
        level = "warn"
        detail.append((f"NOT HIGHLIGHTED: {ctx['blocked_reason']}", "warn", "mid"))
    elif hl is None:
        if ctx.get("pending"):
            detail.append(("highlighting in Fusion…", "info", "mid"))
    else:
        board = ctx.get("board_name") or "board"
        if hl.highlighted:
            detail.append((f"HIGHLIGHTED on {board}: {' '.join(hl.highlighted)}", "ok", "mid"))
        if hl.not_on_board:
            level = "warn"
            detail.append((f"NOT ON BOARD: {' '.join(hl.not_on_board)}", "warn", "mid"))
        if hl.error:
            lv = "warn" if (hl.ok or "AMBIGUOUS_BOARD" in hl.error) else "error"
            level = lv if level == "ok" or lv == "error" else level
            detail.append((f"FUSION: {hl.error}", lv, "mid"))
        if not (hl.highlighted or hl.not_on_board or hl.error):
            level = "warn"
            detail.append(("Fusion highlighted nothing", "warn", "mid"))
    # The match itself is always big and green; problems go in the (amber/red) detail lines.
    out = [(f"✓ {pn}", "ok", "big")]
    if desc:
        out.append((desc, "info", "mid"))
    out.append((desigs, "ok", "big"))
    return out + detail


def hud_sections(resolution: Resolution | None, highlight: HighlightResult | None = None,
                 ctx: dict | None = None) -> tuple[list[Item], list[Item]]:
    ctx = ctx or {}
    return _project(ctx), _result(resolution, highlight, ctx)


def hud_lines(resolution: Resolution | None, highlight: HighlightResult | None = None,
              ctx: dict | None = None) -> list[Line]:
    """Result lines first (what the test/user cares about), then the project lines."""
    top, bottom = hud_sections(resolution, highlight, ctx)
    return [(t, lv) for t, lv, _s in bottom + top]
