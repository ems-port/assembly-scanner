"""Threaded OpenCV camera capture (macOS / AVFoundation).

``Camera(index)`` runs a background thread that keeps only the newest frame.
Consumers call ``latest()`` -> (seq, frame) and skip frames they have already seen,
so a slow decoder never builds a backlog and the preview never lags.

Camera choice is by OpenCV index (config ``camera_index``). ``list_cameras()`` names
the indices from AVFoundation: OpenCV's AVFoundation backend orders devices by
uniqueID. Verified on 7 Oct by correlating frames from each OpenCV index with frames
from each named device (Qt): 0 = LG UltraFine (r=0.99), 1 = MacBook Pro Camera (bench,
r=0.53; its native frame is square, OpenCV's is a 16:9 crop), 2 = iPhone (r=0.97).

Camera permission (TCC): OpenCV's own permission request runs on the capture thread,
cannot spin the main run loop there and never shows the prompt. So we set
OPENCV_AVFOUNDATION_SKIP_AUTH=1 and the UI calls ``request_access()`` on the MAIN
thread; the capture thread is only started once ``auth_status() == "authorized"``.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")   # before cv2 opens a camera

import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

AUTH_NAMES = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized"}
DENIED_HELP = ("Camera access denied. Open System Settings > Privacy & Security > Camera, "
               "turn on 'Assembly Scanner' (or the Terminal app if you started it from a "
               "terminal), then relaunch.")
WAITING_HELP = "Waiting for camera permission: click 'Allow' in the macOS prompt."

try:  # pyobjc-framework-AVFoundation
    import AVFoundation as _AV
except Exception:  # pragma: no cover
    _AV = None


def auth_status() -> str:
    """'authorized' | 'denied' | 'restricted' | 'not determined' | 'unknown'. Never prompts."""
    if _AV is None:
        return "unknown"
    try:
        return AUTH_NAMES.get(int(_AV.AVCaptureDevice.authorizationStatusForMediaType_(
            _AV.AVMediaTypeVideo)), "unknown")
    except Exception:
        return "unknown"


def request_access(callback: Callable[[bool], None]) -> bool:
    """Show the macOS camera prompt. Call on the MAIN thread. ``callback(granted)`` runs on
    an arbitrary thread (marshal it to the UI, e.g. via a Qt signal). Returns False if
    AVFoundation is unavailable (callback not called)."""
    if _AV is None:
        return False
    def handler(granted) -> None:   # must return None (void block)
        try:
            callback(bool(granted))
        except Exception:
            pass

    _AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(_AV.AVMediaTypeVideo, handler)
    return True


@dataclass(frozen=True)
class CameraInfo:
    index: int
    name: str
    unique_id: str


def list_cameras() -> list[CameraInfo]:
    """Cameras in OpenCV index order (video + muxed devices sorted by uniqueID). Never prompts."""
    if _AV is None:
        return []
    try:
        D = _AV.AVCaptureDevice
        devs = list(D.devicesWithMediaType_(_AV.AVMediaTypeVideo)) + \
            list(D.devicesWithMediaType_(_AV.AVMediaTypeMuxed))
        devs.sort(key=lambda d: str(d.uniqueID()))
        return [CameraInfo(i, str(d.localizedName()), str(d.uniqueID())) for i, d in enumerate(devs)]
    except Exception:
        return []


class Camera:
    """Background capture thread. Thread-safe ``latest()``; ``error`` holds a user-facing
    message (None when fine); ``state`` is one of opening / running / waiting-permission /
    error / stopping / stopped."""

    def __init__(self, index: int, width: int = 1920, height: int = 1080, fps: int = 30):
        self.index = index
        self.req = (width, height, fps)
        self.size: tuple[int, int] | None = None
        self.fps_measured = 0.0
        self.state = "stopped"
        self.error: str | None = None
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restart_requested = False

    # -- public
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            if self._stop.is_set():
                self._restart_requested = True
            return
        self._restart_requested = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.index}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        alive = bool(self._thread and self._thread.is_alive())
        with self._lock:
            self._frame = None
        self.state = "stopping" if alive else "stopped"

    def latest(self) -> tuple[int, np.ndarray | None]:
        with self._lock:
            return self._seq, self._frame

    # -- thread
    def _open(self) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            cap.release()
            return None
        w, h, fps = self.req
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        return cap

    def _run(self) -> None:
        cap = None
        try:
            self.state, self.error = "opening", None
            st = auth_status()
            if st in ("denied", "restricted"):
                self.state, self.error = "error", DENIED_HELP
                return
            if st == "not determined":
                # Never let OpenCV ask from this thread; the UI requests on the main thread.
                self.state, self.error = "waiting-permission", WAITING_HELP
                return
            cap = self._open()
            if cap is None:
                self.state = "error"
                self.error = (f"Cannot open camera {self.index} (permission: {st}). "
                              "Check the camera index, or that no other app holds it.")
                return
            self.state, self.error = "running", None
            fails, t_last, n = 0, time.monotonic(), 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 50:
                        self.state, self.error = "error", f"Camera {self.index} stopped delivering frames."
                        return
                    time.sleep(0.02)
                    continue
                fails = 0
                with self._lock:
                    self._frame = frame
                    self._seq += 1
                if self.size is None:
                    self.size = (frame.shape[1], frame.shape[0])
                n += 1
                now = time.monotonic()
                if now - t_last >= 1.0:
                    self.fps_measured, n, t_last = n / (now - t_last), 0, now
        finally:
            if cap is not None:
                cap.release()
            if self._restart_requested:
                # Stop may time out while VideoCapture.read() unwinds. Restart only after
                # the old capture has been released so it cannot block the new camera.
                self._thread = None
                self._restart_requested = False
                self.start()
