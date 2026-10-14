"""PySide6 window: camera preview with a HUD overlay (UI-4), BOM loading, Fusion link.

Threads
  camera thread   app.camera.Camera, keeps only the newest frame
  ScanWorker      QThread: newest frame -> app.main.Controller.step_frame (decode ->
                  resolve -> Debouncer); emits events to the UI thread
  fusion worker   one-thread executor; every FusionClient call runs there (they block)
  UI thread       paints the preview + HUD, owns the widgets

The scan/fire/highlight rules live in the Qt-free app.main.Controller; the HUD text lives
in the Qt-free app.hud. This file only wires threads and paints.

Fusion rules (application behavior + UI-1..3): only a fired MATCH calls highlight(); no
highlight while the board is ambiguous / unchosen / closed (HUD warning instead); a bag
leaving view changes nothing; board choice is "Auto" on every launch, a manual pick holds
for the session only (never saved).
No pop-ups: every status and error is HUD text. The BOM file picker is the only dialog.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget, QApplication)

from app import scanner
from app.camera import DENIED_HELP, WAITING_HELP, Camera, auth_status, list_cameras, request_access
from app.hud import hud_lines, hud_sections  # noqa: F401  (hud_lines re-exported)
from app.main import Controller
from app.models import Bom, HighlightResult, Status

# --- collaborators optional collaborators; stubs keep the UI usable without them ----------
try:
    from app.bom import load_bom
except ImportError:  # pragma: no cover
    def load_bom(path, sheet=None, column_map=None):
        raise RuntimeError("app/bom.py is not available yet")
try:
    from app.fusion_client import FusionClient
except ImportError:  # pragma: no cover
    FusionClient = None


class _StubFusion:
    """Used when app/fusion_client.py is missing: reports 'not connected', never highlights."""
    mode, target, last_latency_ms = "auto", None, None
    _err = "app/fusion_client.py not available"

    def __init__(self, url=None):
        pass

    def status(self):
        return {"connected": False, "doc_name": None, "is_electronics": False, "error": self._err}

    def list_boards(self):
        raise RuntimeError(self._err)

    def select_board(self, doc):
        raise RuntimeError(self._err)

    def auto_select_for(self, designators, pin=True, boards=None):
        raise RuntimeError(self._err)

    def highlight(self, designators):
        return HighlightResult(ok=False, error=self._err)


COLORS = {"ok": QColor("#3ee07a"), "warn": QColor("#ffb020"), "error": QColor("#ff4d4d"),
          "info": QColor("#f2f2f2"), "dim": QColor("#9a9a9a")}


def bom_placeable_designators(bom: Bom) -> list[str]:
    seen: list[str] = []
    for ln in bom.lines:
        if ln.fitted:
            for d in ln.designators:
                if d not in seen:
                    seen.append(d)
    return seen


def bom_summary(bom: Bom) -> str:
    """'[Basket, Tuning kit] · 24 placements · 16 extras' (file name not included)."""
    rep = getattr(bom, "report", None)
    if rep is not None:
        sheets = [s.sheet for s in rep.sheets if getattr(s, "lines", 0)]
        return f"[{', '.join(sheets)}] · {rep.placement_pns} placements · {rep.extra_pns} extras"
    sheets = sorted({ln.sheet for ln in bom.lines})
    pns = bom.by_mouser
    placed = sum(1 for ls in pns.values() if any(l.fitted and l.designators for l in ls))
    return f"[{', '.join(sheets)}] · {placed} placements · {len(pns) - placed} extras"


# --- background workers ----------------------------------------------------------------------
class ScanWorker(QObject):
    """Decode loop with a stall watchdog.

    The loop runs on a plain daemon thread. Each loop thread has a generation number; only
    the current generation may touch the Controller state or emit. ``check_stall()`` (called
    by a UI timer) abandons a thread whose decode has run longer than STALL_S and starts a
    fresh one. A native call cannot be killed, so the stuck thread is left to finish (or
    spin) on its own; whatever it returns later is dropped. Signals:
    live(dict) ~5 Hz: codes, ms, t (monotonic time of the result)
    event(Event) when something fired
    stalled(dict) when a thread was abandoned: count, ms"""
    live = Signal(object)
    event = Signal(object)
    stalled = Signal(object)
    STALL_S = 1.5

    def __init__(self, controller: Controller, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.camera: Camera | None = None
        self._running = True
        self._paused = False
        self._gen = 0
        self._lock = threading.Lock()
        self._busy_since: float | None = None   # start of the current gen's decode
        self.stall_count = 0
        self._thread: threading.Thread | None = None
        self.flip_camera = False

    def start(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._busy_since = None
        self._thread = threading.Thread(target=self._loop, args=(gen,), name=f"scan-{gen}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._gen += 1
        if self._thread is not None:
            self._thread.join(1.0)

    def set_paused(self, paused: bool) -> None:
        """Invalidate an in-flight decode while the window is idle/backgrounded."""
        with self._lock:
            if getattr(self, "_paused", False) == paused:
                return
            self._paused = paused
            self._gen += 1
            gen = self._gen
            self._busy_since = None
        if not paused and self._running:
            self._thread = threading.Thread(target=self._loop, args=(gen,),
                                            name=f"scan-{gen}", daemon=True)
            self._thread.start()

    def check_stall(self) -> None:
        with self._lock:
            since = self._busy_since
        if since is None:
            return
        age = time.monotonic() - since
        if age > self.STALL_S:
            self.stall_count += 1
            self._spawn()           # abandon the stuck generation
            self.stalled.emit({"count": self.stall_count, "ms": age * 1000})

    def _current(self, gen: int) -> bool:
        return self._running and not self._paused and gen == self._gen

    def _loop(self, gen: int) -> None:
        last_seq, last_emit, hist = -1, 0.0, []
        while self._current(gen):
            cam = self.camera
            seq, frame = cam.latest() if cam is not None else (last_seq, None)
            if frame is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            t0 = time.perf_counter()
            with self._lock:
                if gen != self._gen:
                    return
                self._busy_since = time.monotonic()
            try:
                scan_frame = cv2.flip(frame, 1) if self.flip_camera else frame
                codes = self.ctl.decode_frame(scan_frame)
                err = None
            except Exception as e:  # never kill the loop; show it
                codes, err = [], f"decode error: {type(e).__name__}: {e}"
            with self._lock:
                if gen != self._gen:
                    return            # abandoned while decoding: drop the result
                self._busy_since = None
            if err:
                self.live.emit({"error": err, "t": time.monotonic()})
                time.sleep(0.2)
                continue
            ms = (time.perf_counter() - t0) * 1000
            ev = self.ctl.step_codes(codes, time.monotonic(), run_fusion=False)
            ev.decode_ms = ms
            if ev.fired is not None:
                self.event.emit(ev)
            hist = (hist + [ms])[-30:]
            now = time.monotonic()
            if now - last_emit > 0.2:
                last_emit = now
                st = ev.resolution.status if ev.resolution is not None else None
                self.live.emit({"codes": ev.codes, "ms": sum(hist) / len(hist), "t": now,
                                "status": st})


class _Bridge(QObject):
    done = Signal(object, object, object)   # callback, result, error


class FusionWorker:
    """Runs FusionClient calls on one background thread; callbacks run on the UI thread."""
    PAUSED_ERROR = "Fusion request skipped while Assembly Scanner is paused"
    CIRCUIT_ERROR = "Fusion requests stopped after a recursion error; use Rescan boards to retry once Fusion is ready"

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fusion")
        self._bridge = _Bridge()
        self._bridge.done.connect(self._finish)
        self.pending = 0
        self._paused = False
        self._recursion_blocked = False
        self._state_lock = threading.Lock()

    def set_paused(self, paused: bool) -> None:
        with self._state_lock:
            self._paused = paused

    def reset_recursion_block(self) -> None:
        """Allow one explicit retry after the user has reset/recovered Fusion."""
        with self._state_lock:
            self._recursion_blocked = False

    def _finish(self, cb, r, e) -> None:
        self.pending -= 1
        cb(r, e)

    def submit(self, fn, cb) -> None:
        self.pending += 1

        def job():
            with self._state_lock:
                paused = self._paused
                recursion_blocked = self._recursion_blocked
            if paused:
                r, e = None, self.PAUSED_ERROR
            elif recursion_blocked:
                r, e = None, self.CIRCUIT_ERROR
            else:
                try:
                    r, e = fn(), None
                    if "maximum recursion depth" in str(r).lower():
                        with self._state_lock:
                            self._recursion_blocked = True
                except Exception as ex:  # surfaced in the HUD
                    r, e = None, f"{type(ex).__name__}: {ex}"
                    if isinstance(ex, RecursionError) or "maximum recursion depth" in str(ex).lower():
                        with self._state_lock:
                            self._recursion_blocked = True
            self._bridge.done.emit(cb, r, e)
        self._pool.submit(job)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- preview + HUD -------------------------------------------------------------------------
class Preview(QWidget):
    """Camera image with HUD text painted on top (QPainter, not burned into frames)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 400)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image: QImage | None = None
        self.roi: tuple[int, int, int, int] | None = None
        self.top: list[tuple[str, str, str]] = []
        self.bottom: list[tuple[str, str, str]] = []
        self.status_line = ""
        self.center_msg: tuple[str, str] | None = None

    def set_frame(self, frame) -> None:
        h, w = frame.shape[:2]
        self.image = QImage(frame.data, w, h, frame.strides[0], QImage.Format_BGR888).copy()
        self.roi = scanner.roi_rect(w, h)
        self.update()

    @staticmethod
    def _font(px: float, bold=False) -> QFont:
        f = QFont("Menlo")
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(max(9, int(px)))
        f.setBold(bold)
        return f

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#000"))
        if self.image is not None:
            iw, ih = self.image.width(), self.image.height()
            s = max(W / iw, H / ih)      # crop to fill: HUD text sits on the image (UI-4)
            ox, oy = (W - iw * s) / 2, (H - ih * s) / 2
            p.drawImage(QRectF(ox, oy, iw * s, ih * s), self.image)
            if self.roi:
                x, y, w, h = self.roi
                p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.DashLine))
                p.drawRect(QRectF(ox + x * s, oy + y * s, w * s, h * s))

        base = max(11.0, min(W / 50, 17.0))
        size_px = {"small": base, "mid": base * 1.2, "big": base * 2.6}
        pad = base * 0.6

        # top band: project lines + dim status line (elided, one line each)
        top = list(self.top) + ([(self.status_line, "dim", "small")] if self.status_line else [])
        fh = QFontMetrics(self._font(base)).height()
        top_h = pad * 2 + fh * len(top)
        p.fillRect(QRectF(0, 0, W, top_h), QColor(0, 0, 0, 170))
        y = pad
        for text, lvl, _sz in top:
            f = self._font(base * (0.85 if lvl == "dim" else 1.0), bold=lvl != "dim")
            p.setFont(f)
            p.setPen(COLORS.get(lvl, COLORS["info"]))
            el = QFontMetrics(f).elidedText(text, Qt.ElideRight, int(W - 2 * pad))
            p.drawText(QRectF(pad, y, W - 2 * pad, fh), Qt.AlignLeft | Qt.AlignVCenter, el)
            y += fh

        if self.center_msg:
            text, lvl = self.center_msg
            p.setFont(self._font(base * 1.1, True))
            r = QRectF(W * 0.08, top_h + pad, W * 0.84, H * 0.4)
            p.fillRect(r, QColor(0, 0, 0, 160))
            p.setPen(COLORS.get(lvl, COLORS["info"]))
            p.drawText(r.adjusted(pad, pad, -pad, -pad), Qt.AlignCenter | Qt.TextWordWrap, text)

        # bottom band: current result (word-wrapped, max 3 lines per item)
        if self.bottom:
            rows = []
            for text, lvl, sz in self.bottom:
                f = self._font(size_px.get(sz, base), bold=sz != "mid" or lvl != "info")
                fm = QFontMetrics(f)
                br = fm.boundingRect(0, 0, int(W - 2 * pad), 10000, Qt.TextWordWrap, text)
                rows.append((text, lvl, f, min(br.height(), fm.height() * 3)))
            bh = sum(r[3] for r in rows) + pad * 2
            p.fillRect(QRectF(0, H - bh, W, bh), QColor(0, 0, 0, 180))
            y = H - bh + pad
            for text, lvl, f, hh in rows:
                p.setFont(f)
                p.setPen(COLORS.get(lvl, COLORS["info"]))
                p.drawText(QRectF(pad, y, W - 2 * pad, hh), Qt.AlignLeft | Qt.TextWordWrap, text)
                y += hh
        p.end()


class BoardCombo(QComboBox):
    aboutToShow = Signal()

    def showPopup(self):
        self.aboutToShow.emit()
        super().showPopup()


# --- main window -------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    POLL_MS = 15000
    camAccess = Signal(bool)      # AVFoundation completion handler -> UI thread

    def __init__(self, settings, fusion=None, camera_factory=Camera):
        super().__init__()
        self._manual_paused = False
        self._background_paused = QApplication.applicationState() != Qt.ApplicationActive
        self.settings = settings
        self.camera_factory = camera_factory
        self.setWindowTitle("Assembly Scanner")
        self.resize(780, 660)

        url = getattr(settings, "fusion_url", None)
        if fusion is None:
            fusion = (FusionClient(url) if url else FusionClient()) if FusionClient else _StubFusion()
        self.fusion = fusion
        self.ctl = Controller(fusion=fusion)
        self.fworker = FusionWorker()

        self.bom: Bom | None = None
        self.bom_error: str | None = None
        self.fusion_status: dict = {}
        self._poll_in_flight = False
        self._poll_failures = 0
        self._poll_retry_at = 0.0
        self._last_poll_error: str | None = None
        self._same_poll_errors = 0
        self._poll_circuit_open = False
        self.auto_info = None          # (best, coverage, runner_up) from auto_select_for
        self.auto_err: str | None = None
        self.pick_err: str | None = None
        self.live: dict = {}
        self.camera: Camera | None = None
        self.settings_err: str | None = None
        self._last_auto_sig = None
        self._pending_hl = None        # Resolution whose highlight is in flight
        self._rescan_pending = False

        # thin toolbar + preview
        bar = QWidget()
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(6, 4, 6, 4)
        self.btn_bom = QPushButton("Load BOM…")
        self.btn_bom.clicked.connect(self.pick_bom)
        self.btn_copy = QPushButton("Copy result")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self.copy_result)
        self.cam_combo = QComboBox()
        self._invert_item_data = "__invert_camera_image__"
        self._selected_camera_index = int(settings.camera_index)
        self.board_combo = BoardCombo()
        self.board_combo.addItem("Auto", None)
        self.board_combo.aboutToShow.connect(self.poll_fusion)
        self.board_combo.activated.connect(self.on_board_picked)
        self.btn_rescan = QPushButton("Rescan boards")
        self.btn_rescan.clicked.connect(self.rescan_boards)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        for w, stretch in ((self.btn_bom, 0), (self.btn_copy, 0), (QLabel("Camera"), 0), (self.cam_combo, 1),
                           (QLabel("Board"), 0), (self.board_combo, 1), (self.btn_rescan, 0),
                           (self.btn_pause, 0)):
            hl.addWidget(w, stretch)
        self.preview = Preview()
        central = QWidget()
        vl = QVBoxLayout(central)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(bar)
        vl.addWidget(self.preview, 1)
        self.setCentralWidget(central)

        # cameras: OpenCV index + best-guess name (see app/camera.py)
        cams = list_cameras()
        names = {c.index: c.name for c in cams}
        idxs = sorted(set(names) | {0, 1, 2} | {int(settings.camera_index)})
        for i in idxs:
            self.cam_combo.addItem(f"{i}: {names.get(i, 'camera')}", i)
        self.cam_combo.setCurrentIndex(idxs.index(int(settings.camera_index)))
        self.cam_combo.addItem("", self._invert_item_data)
        self._camera_inverted = bool(getattr(settings, "camera_inverted", False))
        self._update_invert_item()
        self.cam_combo.activated.connect(self.on_camera_combo_activated)

        self._asked_permission = False
        self.camAccess.connect(self._on_cam_access)

        self.worker = ScanWorker(self.ctl, self)
        self.worker.flip_camera = self._camera_inverted
        self.worker.live.connect(self.on_live)
        self.worker.event.connect(self.on_event)
        self.worker.stalled.connect(self.on_stalled)
        self.worker.start()
        self.stall_info: dict | None = None
        self._label_seen_t = 0.0
        self.open_camera(int(settings.camera_index))

        if settings.bom_path:
            self.load_bom_file(settings.bom_path, settings.bom_sheet, remember=False)

        self.t_preview = QTimer(self, interval=33, timeout=self.tick_preview)
        self.t_preview.start()
        self.t_poll = QTimer(self, interval=self.POLL_MS, timeout=self.poll_fusion)
        self.t_poll.start()
        self.t_watch = QTimer(self, interval=250, timeout=self.worker.check_stall)
        self.t_watch.start()
        self.refresh_hud()
        QApplication.instance().applicationStateChanged.connect(self._on_application_state)
        self._apply_idle_state()

    def _on_pause_toggled(self, paused: bool) -> None:
        self._manual_paused = paused
        self.btn_pause.setText("Resume" if paused else "Pause")
        self._apply_idle_state()

    def _on_application_state(self, state) -> None:
        self._background_paused = state != Qt.ApplicationActive
        self._apply_idle_state()

    def _is_idle_paused(self) -> bool:
        return self._manual_paused or self._background_paused

    def _apply_idle_state(self) -> None:
        """Stop camera capture and decoding while paused or outside the foreground."""
        paused = self._is_idle_paused()
        self.fworker.set_paused(paused)
        self.worker.set_paused(paused)
        if paused:
            if self.camera:
                self.camera.stop()
            if hasattr(self, "t_preview"):
                self.t_preview.stop()
            if hasattr(self, "t_poll"):
                self.t_poll.stop()
            if hasattr(self, "t_watch"):
                self.t_watch.stop()
            self.preview.center_msg = ("Paused — return to the foreground or press Resume", "dim")
        else:
            if self.camera and self.camera.state in ("stopped", "stopping"):
                self.camera.start()
            if hasattr(self, "t_preview"):
                self.t_preview.start()
            if hasattr(self, "t_poll"):
                self.t_poll.start()
            if hasattr(self, "t_watch"):
                self.t_watch.start()
            if self._rescan_pending:
                self.rescan_boards()
            else:
                self.poll_fusion()
        self.refresh_hud()

    # ---------------------------------------------------------------- camera
    def _update_invert_item(self) -> None:
        i = self.cam_combo.findData(self._invert_item_data)
        if i >= 0:
            state = bool(getattr(self, "_camera_inverted",
                                 getattr(self.settings, "camera_inverted", False)))
            self.cam_combo.setItemText(i, f"Invert image: {'On' if state else 'Off'}")

    def on_camera_combo_activated(self, _index: int) -> None:
        choice = self.cam_combo.currentData()
        if choice == self._invert_item_data:
            self._camera_inverted = not self._camera_inverted
            self.settings.camera_inverted = self._camera_inverted
            self.worker.flip_camera = self._camera_inverted
            self._update_invert_item()
            self.cam_combo.setCurrentIndex(self.cam_combo.findData(self._selected_camera_index))
            self._save_settings()
            self.tick_preview()
            return
        self._selected_camera_index = int(choice)
        self.open_camera(self._selected_camera_index)

    def open_camera(self, index: int) -> None:
        """Start capture only once camera access is authorised. The permission prompt is
        requested here, on the main thread (OpenCV's own request from the capture thread
        never shows a prompt)."""
        if self.camera is not None:
            self.camera.stop()
        self.camera = self.camera_factory(int(index))
        self.worker.camera = self.camera
        self.preview.image = None
        if int(self.settings.camera_index) != int(index):
            self.settings.camera_index = int(index)
            self._save_settings()
        st = auth_status() if self.camera_factory is Camera else "authorized"
        if st == "not determined":
            self.camera.state, self.camera.error = "waiting-permission", WAITING_HELP
            if not self._asked_permission:
                self._asked_permission = True
                if not request_access(self.camAccess.emit):
                    self.camera.state, self.camera.error = "error", "AVFoundation (pyobjc) not available"
            return
        if st in ("denied", "restricted"):
            self.camera.state, self.camera.error = "error", DENIED_HELP
            return
        if self._is_idle_paused():
            self.camera.state = "stopped"
            return
        self.camera.start()

    def _on_cam_access(self, granted: bool) -> None:
        if self.camera is None:
            return
        if granted and not self._is_idle_paused():
            self.camera.start()
        elif granted:
            self.camera.state = "stopped"
        else:
            self.camera.state, self.camera.error = "error", DENIED_HELP
        self.refresh_hud()

    def tick_preview(self) -> None:
        cam = self.camera
        if cam is None:
            return
        _seq, frame = cam.latest()
        if frame is not None and cam.state == "running":
            self.preview.set_frame(cv2.flip(frame, 1) if self.worker.flip_camera else frame)
            self.preview.center_msg = None
        elif cam.error:
            self.preview.center_msg = (cam.error, "warn" if cam.state == "waiting-permission" else "error")
        elif cam.state in ("opening", "waiting-permission"):
            self.preview.center_msg = (f"Opening camera {cam.index}…", "dim")
        self.refresh_hud()

    # ---------------------------------------------------------------- BOM
    def pick_bom(self) -> None:
        start = str(Path(self.settings.bom_path).parent) if self.settings.bom_path else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Load BOM", start,
                                              "BOM files (*.xlsx *.xlsm *.csv);;All files (*)")
        if path:
            sheet = self.settings.bom_sheet if path == self.settings.bom_path else None
            self.load_bom_file(path, sheet, remember=True)

    def load_bom_file(self, path: str, sheet=None, remember=True) -> None:
        try:
            bom = load_bom(path, sheet=sheet)
        except Exception as e:
            self.bom_error = f"cannot load {Path(path).name}: {e}"
            self.refresh_hud()
            return
        self.bom, self.bom_error = bom, None
        self.ctl.set_bom(bom)
        self.btn_copy.setEnabled(False)
        self.btn_copy.setText("Copy result")
        self._last_auto_sig = None
        if remember:
            self.settings.bom_path, self.settings.bom_sheet = path, sheet
            self._save_settings()
        if self.fusion.mode == "auto":
            # Let the normal status/list poll auto-pick once Fusion is responsive.
            # Running an additional script here just repeats a failure during outages.
            self.poll_fusion(force=True)
        self.refresh_hud()

    def _save_settings(self) -> None:
        self.settings_err = self.settings.save()

    # ---------------------------------------------------------------- Fusion: board choice
    def poll_fusion(self, force: bool = False, reset_recursion: bool = False) -> None:
        if self._is_idle_paused():
            return
        if reset_recursion:
            self.fworker.reset_recursion_block()
            self._poll_circuit_open = False
            self._same_poll_errors = 0
            self._last_poll_error = None
            self._poll_failures = 0
            self._poll_retry_at = 0.0
        elif force:
            self._poll_retry_at = 0.0
        if self._poll_in_flight:
            return
        if self._poll_circuit_open or (not force and time.monotonic() < self._poll_retry_at):
            return
        client = self.fusion
        self._poll_in_flight = True

        def job():
            st = client.status()
            boards = st.get("open_boards") if st.get("connected") else None
            if st.get("connected") and boards is None:
                # Compatibility with alternate Fusion clients that don't include boards
                # in status(). The built-in client returns them in the same script call.
                try:
                    boards = client.list_boards()
                except Exception as e:
                    st = st | {"connected": False, "error": str(e)}
            return st, boards
        self.fworker.submit(job, self._on_poll)

    def _on_poll(self, r, err) -> None:
        self._poll_in_flight = False
        if self._is_idle_paused() and err == FusionWorker.PAUSED_ERROR:
            return
        st, boards = ({"connected": False, "error": err}, None) if err else r
        self.fusion_status = st
        if st.get("connected") and boards is not None:
            self._poll_failures = 0
            self._poll_retry_at = 0.0
            self._last_poll_error = None
            self._same_poll_errors = 0
        else:
            self._poll_failures += 1
            message = str(st.get("error") or "Fusion returned no board list")
            if message == self._last_poll_error:
                self._same_poll_errors += 1
            else:
                self._last_poll_error, self._same_poll_errors = message, 1
            if "RecursionError" in message or "maximum recursion depth" in message.lower():
                self._poll_circuit_open = True
                self._poll_retry_at = float("inf")
            else:
                delay = min(60.0, 15.0 * (2 ** min(self._poll_failures - 1, 2)))
                self._poll_retry_at = time.monotonic() + delay
        if boards is not None:
            self._set_board_items(boards)
            # auto mode: (re)pick when the BOM or the set of open boards changed
            if self.fusion.mode == "auto" and self.bom is not None:
                sig = (id(self.bom), tuple(sorted(str(b.get("key", "")) for b in boards)))
                if sig != self._last_auto_sig:
                    self._last_auto_sig = sig
                    self.run_auto_select(boards=boards)
        self.update_board_gate()
        if self._rescan_pending and not self._is_idle_paused():
            # The outstanding call may itself have reopened the recursion breaker.
            # Clear it only after that result is received, then issue one fresh call.
            self._rescan_pending = False
            self.rescan_boards()

    def _set_board_items(self, boards: list[dict]) -> None:
        cur = self.board_combo.currentData()
        self.board_combo.blockSignals(True)
        self.board_combo.clear()
        self.board_combo.addItem("Auto", None)
        for b in boards:
            self.board_combo.addItem(str(b.get("ui_name", b.get("name", "?"))), b.get("key"))
        i = self.board_combo.findData(cur) if cur is not None else 0
        if cur is not None and i < 0:      # pinned board closed: keep showing it, no fallback
            pin = self.fusion.target or {}
            self.board_combo.addItem(f"{pin.get('name', cur)} (closed)", cur)
            i = self.board_combo.count() - 1
        self.board_combo.setCurrentIndex(max(i, 0))
        self.board_combo.blockSignals(False)

    def rescan_boards(self) -> None:
        """Re-list boards; in auto mode also re-run the BOM coverage pick."""
        if self._is_idle_paused():
            self.pick_err = "Resume Assembly Scanner before rescanning; paused mode sends no Fusion requests"
            self.update_board_gate()
            return
        self._last_auto_sig = None
        if self._poll_in_flight:
            self._rescan_pending = True
            self.pick_err = "Rescan queued; waiting for the current Fusion request to finish"
            self.update_board_gate()
            return
        self.pick_err = None
        self.poll_fusion(force=True, reset_recursion=True)

    def on_board_picked(self, _i: int) -> None:
        if self._is_idle_paused():
            return
        key = self.board_combo.currentData()
        client = self.fusion
        self.fworker.submit(lambda: client.select_board(key),
                            lambda r, e: self._after_pick(e, auto=key is None))

    def _after_pick(self, err, auto=False) -> None:
        if self._is_idle_paused() and err == FusionWorker.PAUSED_ERROR:
            return
        self.pick_err = f"cannot select board: {err}" if err else None
        if auto and not err and self.bom is not None:
            self.run_auto_select()
        else:
            self.poll_fusion()
        self.update_board_gate()

    def run_auto_select(self, boards: list[dict] | None = None) -> None:
        if self.bom is None or self._is_idle_paused():
            return
        desigs = set(bom_placeable_designators(self.bom))
        client = self.fusion
        self.fworker.submit(lambda: client.auto_select_for(desigs, boards=boards), self._on_auto)

    def _on_auto(self, r, err) -> None:
        if self._is_idle_paused() and err == FusionWorker.PAUSED_ERROR:
            return
        self.auto_info, self.auto_err = (None, err) if err else (r, None)
        self.update_board_gate()

    def board_ctx(self) -> dict:
        """Board part of the HUD ctx, and whether highlighting is allowed (UI-3)."""
        st, client = self.fusion_status, self.fusion
        mode, tgt = client.mode, client.target
        ctx = {"board_mode": mode, "board_name": (tgt or {}).get("name")}
        if self.pick_err:
            ctx["fusion_error"] = self.pick_err
        elif not st:
            ctx["board_warning"] = "checking Fusion…"
        elif not st.get("connected"):
            reason = f"not connected: {st.get('error') or 'no reply'}"
            if self._poll_circuit_open:
                reason += "; automatic retries paused after repeated recursion errors — click Rescan boards"
            ctx["fusion_error"] = reason
        elif mode == "manual" and st.get("error"):
            ctx["fusion_error"] = f"{st['error']}. Pick another board or Auto."
        elif tgt is None:
            if self.bom is None:
                ctx["board_warning"] = "load a BOM to auto-pick"
            elif self.auto_err:
                ctx["board_warning"] = self.auto_err
            elif self.auto_info and self.auto_info[0]:
                best = self.auto_info[0]
                ctx["board_warning"] = (f"best is {best.get('name')} "
                                        f"({best.get('coverage', 0):.0%}): {best.get('reason')}; pick one")
            elif self.auto_info is not None:
                ctx["board_warning"] = "no Electronics board open in Fusion"
            else:
                ctx["board_warning"] = "choosing…"
        elif mode == "auto" and self.auto_info and self.auto_info[0] \
                and self.auto_info[0].get("key") == tgt.get("key"):
            ctx["board_note"] = f"covers {self.auto_info[1]:.0%} of BOM placements"
        ok = bool(ctx.get("board_name")) and not ctx.get("fusion_error") and not ctx.get("board_warning")
        reason = ctx.get("fusion_error") or ctx.get("board_warning") or ""
        return ctx | {"_ok": ok, "_reason": reason}

    def update_board_gate(self) -> None:
        c = self.board_ctx()
        self.ctl.board_ok, self.ctl.board_block_reason = c["_ok"], c["_reason"]
        self.refresh_hud()

    # ---------------------------------------------------------------- scan events
    def on_live(self, d: dict) -> None:
        self.live = d
        st = d.get("status")
        if st in (Status.MATCH, Status.MATCH_NO_PLACEMENT, Status.NOT_IN_BOM):
            self._label_seen_t = 0.0        # the P/N was read: no hint
        elif len(d.get("codes", [])) >= 2:  # >= 2 symbols, nothing resolves: label present
            self._label_seen_t = time.monotonic()

    def on_stalled(self, info: dict) -> None:
        self.stall_info = info | {"t": time.monotonic()}
        self.refresh_hud()

    def on_event(self, ev) -> None:
        if self._is_idle_paused():
            return
        self._blocked = None
        self.btn_copy.setEnabled(self.ctl.last_fired is not None)
        self.btn_copy.setText("Copy result")
        if ev.action == "blocked":
            self._blocked = self.ctl.board_block_reason or "no usable Fusion board"
        elif ev.action == "highlight":
            res = ev.fired
            self._pending_hl = res
            ctl = self.ctl
            self.fworker.submit(lambda: ctl.do_highlight(res), lambda hr, e: self._on_highlight(res, hr, e))
        self.refresh_hud()

    def copy_result(self) -> None:
        """Copy the latest resolved part and Fusion outcome as plain text."""
        res = self.ctl.last_fired
        if res is None:
            return
        rows = [f"Mouser P/N: {res.mouser_pn or ''}", f"Status: {res.status.value}"]
        mpns = list(dict.fromkeys(line.mpn for line in res.lines if line.mpn))
        descriptions = list(dict.fromkeys(line.description for line in res.lines if line.description))
        if mpns:
            rows.append("Manufacturer P/N: " + ", ".join(mpns))
        if descriptions:
            rows.append("Description: " + " / ".join(descriptions))
        if res.designators:
            designators = ", ".join(res.designators)
            if len(res.designators) > 3:
                designators += f"  sum: {len(res.designators)}"
            rows.append("Designators: " + designators)
        if self.ctl.last_highlight is not None:
            hl = self.ctl.last_highlight
            if hl.highlighted:
                rows.append("Highlighted: " + ", ".join(hl.highlighted))
            if hl.not_on_board:
                rows.append("Not on board: " + ", ".join(hl.not_on_board))
            if hl.error:
                rows.append("Fusion: " + hl.error)
            elif res.status == Status.MATCH:
                rows.append("Fusion board: " + str((self.fusion.target or {}).get("name", "")))
        QApplication.clipboard().setText("\n".join(rows))
        self.btn_copy.setText("Copied")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("Copy result"))

    def _on_highlight(self, res, hr, err) -> None:
        if self._pending_hl is res:
            self._pending_hl = None
        if self._is_idle_paused() and err == FusionWorker.PAUSED_ERROR:
            return
        if err:  # do_highlight never raises; belt and braces
            hr = HighlightResult(ok=False, error=err)
            if res is self.ctl.last_fired:
                self.ctl.last_highlight = hr
        if hr and hr.error and "BOARD_NOT_OPEN" in hr.error:
            if self.fusion.mode == "auto":
                self.rescan_boards()        # auto target closed: re-pick (this scan is not retried)
            else:
                self.poll_fusion()
        self.refresh_hud()

    # ---------------------------------------------------------------- HUD
    def hud_ctx(self) -> dict:
        ctx = self.board_ctx()
        if self.bom is not None:
            ctx["bom_name"] = Path(self.bom.source_path).name
            ctx["bom_summary"] = bom_summary(self.bom)
        if self.bom_error:
            ctx["bom_error"] = self.bom_error
        cam = self.camera
        if cam is not None and cam.state == "error":
            ctx["camera_ok"], ctx["camera_error"] = False, cam.error
        else:
            ctx["camera_ok"] = True
        si = self.stall_info
        if si is not None and time.monotonic() - si["t"] < 10:
            ctx["scanner_error"] = (f"STALLED: a decode hung > {si['ms'] / 1000:.1f} s; "
                                    f"restarted the scanner (stall #{si['count']})")
        if si is not None and si["count"] >= 3:
            ctx["scanner_error"] = (f"STALLED {si['count']} times: hung decodes keep a CPU core "
                                    "busy. Quit and relaunch the app.")
        if cam is not None and cam.state == "running" and "t" in self.live \
                and time.monotonic() - self.live["t"] > 3 * ScanWorker.STALL_S:
            ctx.setdefault("scanner_error", "no decode result for "
                           f"{time.monotonic() - self.live['t']:.0f} s while frames arrive")
        # A label is in view but its P/N barcode does not decode (marginal resolution / tilt).
        if self.bom is not None and time.monotonic() - self._label_seen_t < 1.2:
            ctx["label_seen"] = True
        res = self.ctl.last_fired
        if res is not None and res.status == Status.MATCH:
            if getattr(self, "_blocked", None) and self.ctl.last_highlight is None and self._pending_hl is None:
                ctx["blocked_reason"] = self._blocked
            ctx["pending"] = self._pending_hl is res
        return ctx

    def refresh_hud(self) -> None:
        ctx = self.hud_ctx()
        top, bottom = hud_sections(self.ctl.last_fired, self.ctl.last_highlight, ctx)
        bits = []
        cam = self.camera
        if cam is not None:
            name = self.cam_combo.currentText()
            if cam.state == "running" and cam.size:
                bits.append(f"CAM {name} {cam.size[0]}x{cam.size[1]} {cam.fps_measured:.0f}fps")
            else:
                bits.append(f"CAM {name}: {cam.state}")
        age = time.monotonic() - self.live["t"] if "t" in self.live else None
        if self.live.get("error"):
            bits.append(self.live["error"])
        elif "ms" in self.live and age is not None and age <= ScanWorker.STALL_S:
            bits.append(f"decode {self.live['ms']:.0f} ms · {len(self.live.get('codes', []))} codes "
                        f"in view · {age:.1f} s ago")
        elif age is not None and cam is not None and cam.state == "running":
            bits.append(f"no decode result for {age:.0f} s")
        if self.fusion_status.get("connected"):
            lat = getattr(self.fusion, "last_latency_ms", None)
            bits.append("Fusion connected" + (f" ({lat:.0f} ms)" if lat else ""))
        if self.settings_err:
            bits.append(self.settings_err)
        self.preview.top, self.preview.bottom = top, bottom
        self.preview.status_line = " · ".join(bits)
        self.preview.update()

    def closeEvent(self, ev) -> None:
        self.t_preview.stop()
        self.t_poll.stop()
        self.t_watch.stop()
        self.worker.stop()
        if self.camera:
            self.camera.stop()
        self.fworker.shutdown()
        super().closeEvent(ev)
