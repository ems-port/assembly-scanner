"""Fusion link: highlight board parts via Autodesk's stock Fusion MCP server.

Transport: MCP streamable HTTP (JSON-RPC 2.0) at http://127.0.0.1:27182/mcp.
`initialize` returns an MCP-Session-Id header that is sent on every later call, then
`notifications/initialized` is sent. The session is kept and rebuilt once on failure.

Board/document data comes from Fusion's read-only Electronics and document tools. Actions
use `fusion_mcp_execute` scripts with stdout disabled. The highlight is
Schematic.selectObjects(), which Fusion cross-probes onto the board editor (see that
script). Read-only means: no design change, no undo entry, no save/close, and it is
allowed while a command dialog is open.

Board selection (UI-3). Several Electronics boards can be open; a board is identified by
its document key (see list_boards()).
  * Auto mode (the default on every launch): auto_select_for(bom_placements) scores every
    open board and becomes the target only when the pick is confident. If it is not
    confident, highlight() returns an error until the user picks a board.
  * Manual mode: select_board(key_or_name) pins one board for this session (in memory
    only, never persisted). select_board(None) returns to auto mode.
  * A target is never swapped silently. If the target board document has closed,
    highlight()/clear()/board_designators() return an error, never another board.
  * With no target (auto mode before any auto pick), the client acts only when exactly one
    board is open.
  * When the target board is not the active tab, highlight() brings its tab forward
    (Document.activate(): a view change, does not mark the doc modified) so the highlight
    is visible. Pass activate=False to the constructor to disable that.

All public methods block; call them from a worker thread. None of them raise for Fusion
or network problems: errors come back in the result (HighlightResult.error, or
status()["error"]). Details of the last call (board, activated) are in `last_info`.

CLI for manual checks (each run is a new session, so --board pins only for that run):
    .venv/bin/python -m app.fusion_client status
    .venv/bin/python -m app.fusion_client boards
    .venv/bin/python -m app.fusion_client use <board-name>      # resolve + show a board
    .venv/bin/python -m app.fusion_client auto <BOM.xlsx>
    .venv/bin/python -m app.fusion_client auto R1 R2 C5             # or designators
    .venv/bin/python -m app.fusion_client highlight R4 R5 [--board <key|name>] [--auto BOM]
    .venv/bin/python -m app.fusion_client clear [--board ...]
    .venv/bin/python -m app.fusion_client designators [--board ...]
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app import __version__
from app.models import HighlightResult

DEFAULT_URL = os.environ.get("ASCAN_FUSION_URL", "http://127.0.0.1:27182/mcp")
PROTOCOL_VERSION = "2025-03-26"
SCRIPT_PATH = Path(__file__).with_name("fusion_scripts") / "assembly_scanner.py"
MARK = "ASCAN_JSON:"

# Auto-pick defaults: require strong coverage and a clear lead over the next board.
AUTO_MIN_COVERAGE = 0.95
AUTO_MIN_MARGIN = 0.05     # best must beat the runner-up by this much coverage


class FusionError(Exception):
    """Fusion or MCP returned an error, or the reply could not be understood."""


class FusionClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 10.0, zoom: bool = False,
                 activate: bool = True, min_coverage: float = AUTO_MIN_COVERAGE,
                 min_margin: float = AUTO_MIN_MARGIN):
        self.url = url
        self.timeout = timeout
        self.zoom = zoom                  # passed to selectObjects; it zooms the schematic view
        self.activate = activate          # bring the target board tab forward on highlight
        self.min_coverage = min_coverage
        self.min_margin = min_margin
        self._sid: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._script_src = SCRIPT_PATH.read_text(encoding="utf-8")
        self.last_latency_ms: float | None = None
        self.last_info: dict = {}
        self._pinned: dict | None = None      # manual override (session only)
        self._auto: dict | None = None        # last confident auto pick
        self._last_key: str | None = None     # board that holds the current highlight

    # ------------------------------------------------------------------ transport
    def _post(self, body: dict, with_session: bool = True) -> tuple[dict | None, dict]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if with_session and self._sid:
            headers["MCP-Session-Id"] = self._sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
        if not raw.strip():
            return None, hdrs
        if "text/event-stream" in hdrs.get("content-type", "") or raw.lstrip().startswith(("event:", "data:")):
            # SSE: take the last data: payload that parses as a JSON-RPC response.
            msg = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        pass
            return msg, hdrs
        return json.loads(raw), hdrs

    def _connect(self) -> None:
        self._sid = None
        self._id += 1
        msg, hdrs = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                "clientInfo": {"name": "assembly-scanner", "version": __version__}}},
                               with_session=False)
        if not msg or "error" in msg:
            raise FusionError(f"MCP initialize failed: {msg}")
        self._sid = hdrs.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_tool(self, name: str, arguments: dict) -> str:
        """tools/call with one reconnect on failure. Returns the text content."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                if self._sid is None:
                    self._connect()
                self._id += 1
                msg, _ = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
                if msg is None:
                    raise FusionError("empty MCP reply")
                if "error" in msg:
                    # Typically an expired/unknown session: reconnect and retry once.
                    raise FusionError(f"MCP error: {msg['error']}")
                res = msg.get("result", {})
                text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
                if res.get("isError"):
                    raise _ToolError(text or "tool reported isError")
                return text
            except _ToolError:
                raise
            except (urllib.error.URLError, OSError, FusionError, json.JSONDecodeError) as e:
                last = e
                self._sid = None
                if isinstance(e, (TimeoutError,)) or "timed out" in str(e):
                    break   # do not double a timeout; the script may still be running in Fusion
        raise FusionError(f"cannot reach Fusion MCP at {self.url}: {last}")

    def _read_tool(self, name: str, arguments: dict) -> dict:
        """Call a structured read tool; this avoids Fusion's recursive script stdout path."""
        text = self._call_tool(name, arguments)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            raise FusionError(f"unexpected {name} reply: {text[:300]}") from e
        if not isinstance(value, dict):
            raise FusionError(f"unexpected {name} reply: expected an object")
        return value

    def _board_snapshot(self) -> tuple[list[dict], str | None]:
        """Get open documents and active board designators without executing a script."""
        with self._lock:
            docs = self._read_tool("fusion_mcp_read", {
                "queryType": "document", "operation": "open"}).get("results", [])
            boards = self._read_tool("fusion_mcp_electronics_read", {
                "entity_type": "electronics.Board"}).get("items", [])
            elements = self._read_tool("fusion_mcp_electronics_read", {
                "entity_type": "electronics.Element", "object": {
                    "fields": ["name"], "pagination": {"limit": 1000, "offset": 0}}}).get("items", [])
        active_doc = next((d for d in docs if d.get("isActive")), None)
        active_board = boards[0] if boards else None
        board_name = Path(str(active_board.get("name", ""))).stem if active_board else None
        if active_board and not active_doc:
            active_doc = next((d for d in docs
                               if str(d.get("name", "")).casefold() == str(board_name).casefold()), None)
        active_key = (active_doc or {}).get("id")
        designators = [str(e.get("name", "")) for e in elements if e.get("name")]
        counts = {}
        for d in docs:
            n = str(d.get("name", "Untitled document"))
            counts[n.casefold()] = counts.get(n.casefold(), 0) + 1
        seen = {}
        out = []
        for d in docs:
            key = d.get("id")
            is_active_board = bool(active_board and key == active_key)
            name = str(d.get("name", board_name or "Untitled document"))
            duplicate_index = seen.get(name.casefold(), 0) + 1
            seen[name.casefold()] = duplicate_index
            ui_name = name
            if counts.get(name.casefold(), 0) > 1:
                label = "active" if d.get("isActive") else f"open {duplicate_index}"
                ui_name = f"{name} ({label})"
            item = {"key": key, "lineage": key, "version": None,
                    "name": name, "ui_name": ui_name,
                    "has_board": True if is_active_board else None,
                    "has_schematic": True if is_active_board else None,
                    "is_active": bool(d.get("isActive")),
                    "is_modified": bool(d.get("isModified")),
                    "designator_count": len(designators) if is_active_board else 0,
                    "designators": designators if is_active_board else []}
            out.append(item)
        if active_board and not out:
            out.append({"key": "active:" + str(board_name), "lineage": None, "version": None,
                        "name": board_name, "has_board": True, "has_schematic": True,
                        "is_active": True, "is_modified": False,
                        "designator_count": len(designators), "designators": designators})
        return out, active_doc.get("name") if active_doc else board_name


    def _run(self, op: str, designators: list[str] | None = None, target: str | None = None,
             clear_keys: list[str] | None = None) -> dict:
        args = {"op": op, "designators": list(designators or []), "zoom": self.zoom,
                "activate": self.activate, "target": target, "clear_keys": list(clear_keys or []),
                "silent_output": True}
        src = self._script_src.replace("json.loads(__ARGS__)", "json.loads(%r)" % json.dumps(args))
        with self._lock:
            t0 = time.perf_counter()
            try:
                text = self._call_tool("fusion_mcp_execute", {
                    "featureType": "script", "object": {"script": src, "readOnly": True}})
            finally:
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            raise FusionError(f"unexpected reply: {text[:300]}")
        if not env.get("success", False):
            msg = _short_error(env.get("error") or env.get("message") or text)
            if target and self.target and self.target.get("key") == target:
                msg = msg.replace(f"({target})", f"({self.target.get('name')}, {target})")
            raise FusionError(msg)
        out = env.get("message", "")
        for line in out.splitlines():
            if line.startswith(MARK):
                return json.loads(line[len(MARK):])
        if not out.strip() and op in ("highlight", "clear", "activate"):
            # Successful silent actions intentionally have no stdout payload.
            return {"op": op, "board": self.target or {}, "selected_ok": True,
                    "highlighted": list(designators or []), "not_on_board": [],
                    "no_schematic_part": [], "activated": bool(self.activate and op == "highlight")}
        raise FusionError(f"script printed no result: {out[:300]}")

    # ------------------------------------------------------------------ board selection
    @property
    def mode(self) -> str:
        """'manual' when a board is pinned with select_board(), else 'auto'."""
        return "manual" if self._pinned else "auto"

    @property
    def target(self) -> dict | None:
        """The board highlight() acts on: the pinned board, else the last confident auto
        pick, else None (then only a single open board is used)."""
        return self._pinned or self._auto

    def _target_key(self) -> str | None:
        t = self.target
        return t["key"] if t else None

    def list_boards(self) -> list[dict]:
        """Open board documents: key, name, version, lineage, has_board, has_schematic,
        is_active, is_modified, designator_count, designators. Raises FusionError if
        Fusion is unreachable (the UI shows it; there is nothing to fall back to)."""
        return self._board_snapshot()[0]

    def select_board(self, doc: str | None) -> dict | None:
        """Pin a board by key, lineage URN or (unique, case-insensitive) document name, for
        this session only. None returns to auto mode. Returns the pinned board's info, or
        None in auto mode. Raises FusionError if the board is not open or is ambiguous."""
        if doc is None:
            self._pinned = None
            return None
        boards = self.list_boards()
        hit = [b for b in boards if b["key"] == doc] or \
              [b for b in boards if b.get("lineage") == doc] or \
              [b for b in boards if b["name"].casefold() == str(doc).casefold()]
        if len(hit) != 1:
            names = ", ".join(f"{b['name']} [{b['key']}]" for b in boards) or "none"
            raise FusionError(("no open board matches" if not hit else "several open boards match")
                              + f" {doc!r}; open boards: {names}")
        self._pinned = _strip(hit[0])
        try:
            # User selection is explicit; activate that board so the Electronics read tools
            # can supply its placements without using Fusion's script stdout channel.
            self._run("activate", target=self._pinned["key"])
            current, _ = self._board_snapshot()
            active = next((b for b in current if b.get("key") == self._pinned["key"]
                           and b.get("has_board")), None)
            if active is None:
                raise FusionError(f"{self._pinned['name']} is open but is not an Electronics board")
            self._pinned = _strip(active)
        except Exception:
            self._pinned = None
            raise
        return self._pinned

    def auto_select_for(self, designators, pin: bool = True, boards: list[dict] | None = None):
        """Score every open board against the BOM's placeable designators.

        Returns (best, coverage, runner_up):
          best      - board info dict (or None if no board is open) with extra keys
                      coverage, precision, matched, missing, confident, reason
          coverage  - best["coverage"]: |BOM ∩ board| / |BOM|
          runner_up - the next board's info dict with the same keys, or None
        Ranking: coverage, then precision (|BOM ∩ board| / |board|), then the active tab.
        confident = coverage >= min_coverage AND (no runner-up OR coverage - runner-up
        coverage >= min_margin). Only a confident pick becomes the auto target (when pin
        is true); otherwise the auto target is cleared, so highlight() errors until the
        user picks a board. A manual pin is never changed here."""
        want = {str(d).strip().upper() for d in designators if str(d).strip()}
        boards = self.list_boards() if boards is None else boards
        if len(boards) == 1 and not boards[0].get("designators") and not self._pinned:
            # With one open document, probing it is unambiguous. Activate it once so the
            # Electronics read tools can get its BOM coverage without script output.
            try:
                self._run("activate", target=boards[0]["key"])
                boards, _ = self._board_snapshot()
            except FusionError:
                pass
        scored = []
        for b in boards:
            have = {d.upper() for d in b.get("designators", [])}
            m = want & have
            s = _strip(b)
            s.update(coverage=(len(m) / len(want)) if want else 0.0,
                     precision=(len(m) / len(have)) if have else 0.0,
                     matched=len(m), missing=sorted(want - have))
            scored.append(s)
        scored.sort(key=lambda s: (s["coverage"], s["precision"], s["is_active"]), reverse=True)
        if not scored:
            if pin:
                self._auto = None
            return None, 0.0, None
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        reasons = []
        if not want:
            reasons.append("the BOM has no placeable designators")
        if best["coverage"] < self.min_coverage:
            reasons.append(f"best coverage {best['coverage']:.0%} < {self.min_coverage:.0%}")
        if runner and best["coverage"] - runner["coverage"] < self.min_margin:
            reasons.append(f"{runner['name']} covers {runner['coverage']:.0%} too "
                           f"(margin < {self.min_margin:.0%})")
        best["confident"] = not reasons
        best["reason"] = "; ".join(reasons) or "ok"
        if runner:
            runner["confident"], runner["reason"] = False, "runner-up"
        if pin:
            self._auto = best if best["confident"] else None
        return best, best["coverage"], runner

    # ------------------------------------------------------------------ public API
    def status(self) -> dict:
        """{connected, doc_name, is_electronics} plus mode, board (target info), boards
        (count), open_boards, active_doc, error. doc_name is the target board's name if there is one,
        else the single open board's, else None."""
        try:
            boards, active_doc = self._board_snapshot()
        except (FusionError, _ToolError) as e:
            return {"connected": False, "doc_name": None, "is_electronics": False,
                    "mode": self.mode, "board": self.target, "boards": None,
                    "open_boards": [], "error": str(e)}
        err = None
        tgt = self._target_key()
        if tgt:
            cur = [b for b in boards if b["key"] == tgt]
            board = _strip(cur[0]) if cur else None
            if not cur:
                err = f"the selected board ({self.target['name']}) is no longer open in Fusion"
        else:
            board = _strip(boards[0]) if len(boards) == 1 else None
            if len(boards) > 1:
                err = f"{len(boards)} boards open; choose one (auto needs a loaded BOM)"
        return {"connected": True, "doc_name": board["name"] if board else None,
                "is_electronics": any(b.get("has_board") for b in boards),
                "mode": self.mode, "board": board,
                "boards": len(boards), "open_boards": boards,
                "active_doc": active_doc, "error": err}

    def board_designators(self) -> set[str] | None:
        """Element names on the target board; None if no usable board or Fusion is unreachable."""
        try:
            boards, _ = self._board_snapshot()
            target = self._target_key()
            board = next((b for b in boards if b.get("key") == target), None) if target else \
                next((b for b in boards if b.get("is_active") and b.get("has_board")), None)
            if target and (board is None or not board.get("is_active")):
                self._run("activate", target=target)
                boards, _ = self._board_snapshot()
                board = next((b for b in boards if b.get("key") == target), None)
            return set(board.get("designators", [])) if board and board.get("has_board") else None
        except (FusionError, _ToolError, KeyError):
            return None

    def highlight(self, designators) -> HighlightResult:
        """Clear the previous highlight (on whichever board holds it), then highlight these
        designators on the target board."""
        want = [str(d).strip() for d in designators if str(d).strip()]
        tgt = self._target_key()
        try:
            r = self._run("highlight", want, target=tgt,
                          clear_keys=[self._last_key] if self._last_key else [])
            boards, _ = self._board_snapshot()
            board = next((b for b in boards if b.get("key") == tgt), None) if tgt else \
                next((b for b in boards if b.get("is_active") and b.get("has_board")), None)
            if board is None:
                board = r.get("board") or self.target or {}
        except (FusionError, _ToolError) as e:
            self.last_info = {"board": self.target, "activated": False}
            return HighlightResult(ok=False, error=str(e))
        self._last_key = (board or {}).get("key")
        self.last_info = {"board": board, "activated": r.get("activated", False)}
        have = {str(d).upper(): str(d) for d in (board or {}).get("designators", [])}
        highlighted = tuple(have[d.upper()] for d in want if d.upper() in have)
        missing = tuple(d for d in want if d.upper() not in have)
        # The execute script completed and reported no failure. The separate Electronics
        # Part query does not use the same schematic context and falsely marked valid parts
        # as missing, so rely on the script's successful selection and board placement list.
        return HighlightResult(ok=True, highlighted=highlighted, not_on_board=missing)

    def clear(self) -> HighlightResult:
        """Clear the highlight on the target board and on the board that last held one."""
        tgt = self._target_key() or self._last_key
        try:
            r = self._run("clear", target=tgt, clear_keys=[self._last_key] if self._last_key else [])
        except (FusionError, _ToolError) as e:
            return HighlightResult(ok=False, error=str(e))
        self._last_key = None
        self.last_info = {"board": r.get("board"), "activated": False}
        return HighlightResult(ok=True)


class _ToolError(FusionError):
    pass


def _strip(b: dict) -> dict:
    """Board info without the (long) designator list."""
    return {k: v for k, v in b.items() if k != "designators"}


def _short_error(s: str) -> str:
    """Keep the last meaningful line of a Fusion traceback, minus the exception class."""
    lines = [l for l in str(s).strip().splitlines() if l.strip()]
    if not lines:
        return "Fusion script failed"
    last = lines[-1]
    for pfx in ("RuntimeError: ", "ValueError: "):
        if last.startswith(pfx):
            last = last[len(pfx):]
    if "command dialog" in s.lower() or "interactive" in s.lower():
        return "Fusion is busy (a command dialog is open): " + last
    return last


# ---------------------------------------------------------------------- CLI
def _bom_designators(arg: list[str]) -> set[str]:
    """A BOM file path (via app.bom.load_bom) or a list of designators."""
    if len(arg) == 1 and Path(arg[0]).suffix.lower() in {".xlsx", ".xlsm", ".csv"}:
        from app.bom import load_bom
        bom = load_bom(arg[0])
        return {d for ln in bom.lines if ln.fitted for d in ln.designators}
    return set(arg)


def _fmt_board(b: dict | None) -> str:
    if not b:
        return "none"
    s = f"{b['name']} v{b.get('version')} [{b['key']}]"
    if "coverage" in b:
        s += f" coverage {b['coverage']:.0%} ({b['matched']}), precision {b['precision']:.0%}"
        if b.get("missing"):
            s += f", missing {' '.join(b['missing'][:10])}{' ...' if len(b['missing']) > 10 else ''}"
    return s


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.fusion_client")
    ap.add_argument("cmd", choices=["status", "boards", "use", "auto", "designators",
                                    "highlight", "clear", "bench"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--board", help="pin this board (key, lineage or doc name) for this run")
    ap.add_argument("--auto", metavar="BOM", help="auto-pick the board for this BOM first")
    ap.add_argument("--no-activate", action="store_true", help="do not bring the board tab forward")
    ap.add_argument("--zoom", action="store_true", help="also zoom the schematic view to the selection")
    ap.add_argument("-n", type=int, default=10, help="bench: iterations")
    a = ap.parse_args(argv)
    c = FusionClient(a.url, a.timeout, a.zoom, activate=not a.no_activate)
    try:
        if a.board:
            print("pinned:", _fmt_board(c.select_board(a.board)))
        if a.auto:
            best, cov, ru = c.auto_select_for(_bom_designators([a.auto]))
            print("auto:", _fmt_board(best), "| confident" if best and best["confident"]
                  else f"| NOT confident: {best and best['reason']}")
    except FusionError as e:
        print("ERROR:", e)
        return 2
    if a.cmd == "status":
        print(json.dumps(c.status(), indent=2))
    elif a.cmd == "boards":
        for b in c.list_boards():
            print(("* " if b["is_active"] else "  ") + _fmt_board(b),
                  f"{b.get('designator_count')} parts, schematic={b['has_schematic']}, modified={b['is_modified']}")
    elif a.cmd == "use":
        if not a.args:
            ap.error("use <key|name|auto>")
        try:
            b = c.select_board(None if a.args[0] == "auto" else a.args[0])
        except FusionError as e:
            print("ERROR:", e)
            return 2
        print("mode:", c.mode, "| board:", _fmt_board(b))
    elif a.cmd == "auto":
        if not a.args:
            ap.error("auto <BOM file | designators...>")
        best, cov, ru = c.auto_select_for(_bom_designators(a.args))
        print("best:     ", _fmt_board(best))
        print("runner-up:", _fmt_board(ru))
        if best:
            print("confident" if best["confident"] else f"NOT confident: {best['reason']}",
                  f"(min coverage {c.min_coverage:.0%}, min margin {c.min_margin:.0%})")
    elif a.cmd == "designators":
        d = c.board_designators()
        print("none (no usable board / unreachable)" if d is None else " ".join(sorted(d)))
    elif a.cmd == "highlight":
        print(c.highlight(a.args))
        print("board:", _fmt_board(c.last_info.get("board")), "| tab activated:", c.last_info.get("activated"))
    elif a.cmd == "clear":
        print(c.clear())
    elif a.cmd == "bench":
        ds = a.args or ["R4", "R5"]
        c.status()   # warm the session
        ts = []
        for i in range(a.n):
            r = c.highlight(ds) if i % 2 == 0 else c.highlight(["C1"])
            ts.append(c.last_latency_ms)
            if not r.ok:
                print("FAIL", r)
        ts.sort()
        print(f"highlight round trip over {a.n}: min {ts[0]:.0f} ms, median {ts[len(ts)//2]:.0f} ms, max {ts[-1]:.0f} ms")
        c.highlight(ds)
    print(f"[{c.last_latency_ms:.0f} ms]" if c.last_latency_ms is not None else "")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
