"""Assembly Scanner entry point: ``.venv/bin/python -m app.main``.

Settings live in ~/Library/Application Support/AssemblyScanner/config.json:
  bom_path      last BOM file loaded (reloaded on start)
  bom_sheet     sheet name or list, null = every sheet with a Mouser P/N column
  camera_index  OpenCV/AVFoundation index (default 1)
  fusion_url    Fusion MCP endpoint
The Fusion board choice is session-only; the app starts in Auto mode.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("ASCAN_CONFIG_DIR",
                                 Path.home() / "Library" / "Application Support" / "AssemblyScanner"))
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Settings:
    bom_path: str | None = None
    bom_sheet: str | list[str] | None = None
    camera_index: int = 1
    camera_inverted: bool = False
    fusion_url: str = "http://127.0.0.1:27182/mcp"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return cls()

    def save(self, path: Path = CONFIG_PATH) -> str | None:
        """Returns an error message instead of raising (the UI shows it in the HUD)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
            return None
        except OSError as e:
            return f"cannot save settings: {e}"


@dataclass
class Event:
    """What one Controller step produced."""
    codes: list[str]
    resolution: object | None          # live Resolution for this frame (None if no BOM / no codes)
    fired: object | None = None        # Resolution that fired this step, else None
    action: str | None = None          # "highlight" | "blocked" | "status" | None
    highlight: object | None = None    # HighlightResult when the call ran synchronously
    decode_ms: float = 0.0


class Controller:
    """Qt-free glue: decode -> resolve -> debounce -> Fusion highlight (UX rules + UI-1/UI-3).

    * a fired MATCH -> ``fusion.highlight(designators)`` (FusionClient clears the previous
      highlight and highlights the new set in the same call), but only if a board is
      usable (``board_ok``); otherwise action "blocked" and no Fusion call;
    * a fired MATCH_NO_PLACEMENT / NOT_IN_BOM -> action "status", no Fusion call;
    * frames with nothing usable -> nothing (a bag leaving view keeps the highlight).

    ``step_*(..., run_fusion=True)`` makes the Fusion call inline (tests / CLI). The UI passes
    run_fusion=False and runs ``do_highlight`` on its Fusion worker thread instead.
    """

    def __init__(self, fusion=None, bom=None, debouncer=None, decode=None, resolve=None):
        from app.state import Debouncer
        if decode is None:
            from app.scanner import decode
        if resolve is None:
            from app.resolver import resolve
        self.fusion = fusion
        self.bom = bom
        self.debouncer = debouncer or Debouncer()
        self._decode = decode
        self._resolve = resolve
        self.board_ok = True                   # set by the UI from the board state (UI-3)
        self.board_block_reason = ""
        self.last_fired = None
        self.last_highlight = None

    def set_bom(self, bom) -> None:
        self.bom = bom
        self.debouncer.reset()
        self.last_fired = self.last_highlight = None

    def decode_frame(self, frame) -> list[str]:
        """Decode only (no state change), so a caller can abandon a hung decode safely."""
        bom = self.bom
        if bom is None:
            return self._decode(frame)
        return self._decode(frame, stop=lambda c: self._stop_for(c, bom))

    def _stop_for(self, codes, bom) -> bool:
        from app.models import Status
        return self._resolve(codes, bom).status in (Status.MATCH, Status.MATCH_NO_PLACEMENT)

    def step_frame(self, frame, now: float, run_fusion: bool = True) -> Event:
        import time as _t
        t0 = _t.perf_counter()
        codes = self.decode_frame(frame)
        ev = self.step_codes(codes, now, run_fusion)
        ev.decode_ms = (_t.perf_counter() - t0) * 1000
        return ev

    def step_codes(self, codes, now: float, run_fusion: bool = True) -> Event:
        from app.models import Status
        codes = list(codes or [])
        if self.bom is None:
            return Event(codes, None)
        res = self._resolve(codes, self.bom) if codes else None
        fired = self.debouncer.update(res if (res is not None and res.mouser_pn) else None, now)
        ev = Event(codes, res, fired)
        if fired is None:
            return ev
        self.last_fired, self.last_highlight = fired, None
        if fired.status == Status.MATCH:
            if not self.board_ok or self.fusion is None:
                ev.action = "blocked"
            else:
                ev.action = "highlight"
                if run_fusion:
                    ev.highlight = self.do_highlight(fired)
        else:
            ev.action = "status"
        return ev

    def do_highlight(self, resolution):
        """Blocking Fusion call for a fired MATCH. Never raises."""
        from app.models import HighlightResult
        try:
            hr = self.fusion.highlight(list(resolution.designators))
        except Exception as e:  # FusionClient should not raise; a stub might
            hr = HighlightResult(ok=False, error=f"{type(e).__name__}: {e}")
        if hr is None:
            hr = HighlightResult(ok=True, highlighted=tuple(resolution.designators))
        if resolution is self.last_fired:
            self.last_highlight = hr
        return hr


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from app.ui import MainWindow

    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Assembly Scanner")
    app.setOrganizationName("Polystage")
    w = MainWindow(Settings.load())
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
