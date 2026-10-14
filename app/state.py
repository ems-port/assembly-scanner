"""Trigger debouncing. Pure logic with injected time.

``Debouncer.update(resolution | None, now) -> Resolution | None``

* The key is ``resolution.mouser_pn``. ``None`` (or a resolution with no
  mouser_pn, i.e. NO_CANDIDATE) means "no target in this frame".
* A key fires once after it has been seen continuously for ``stable_s``.
* After firing it is ignored until it has been absent for ``rearm_s``; then the
  same bag shown again fires again.
* A different key showing up starts its own stability timer and can fire
  immediately after ``stable_s`` (bag A swapped for bag B), even if A has not
  re-armed yet.
* Short drop-outs (a frame or two without a decode) shorter than ``rearm_s``
  neither re-arm the fired key nor reset the candidate's stability timer, so a
  label that decodes in only some frames still fires.

Timings are TBR-2 (from the brief; tune on real bags).
"""
from __future__ import annotations

from app.models import Resolution


class Debouncer:
    def __init__(self, stable_s: float = 0.25, rearm_s: float = 0.5):
        self.stable_s = stable_s
        self.rearm_s = rearm_s
        self._cand: str | None = None        # key being timed
        self._cand_since: float = 0.0
        self._fired: str | None = None       # last key that fired, until re-armed
        self._last_seen: dict[str, float] = {}

    def reset(self) -> None:
        self.__init__(self.stable_s, self.rearm_s)

    @property
    def fired_key(self) -> str | None:
        return self._fired

    def update(self, resolution: Resolution | None, now: float) -> Resolution | None:
        key = resolution.mouser_pn if resolution is not None else None

        # Re-arm the fired key once it has been absent long enough.
        if self._fired is not None and key != self._fired:
            last = self._last_seen.get(self._fired)
            if last is None or now - last >= self.rearm_s:
                self._fired = None
        # Drop a stale candidate.
        if self._cand is not None and key != self._cand:
            last = self._last_seen.get(self._cand)
            if last is None or now - last >= self.rearm_s:
                self._cand = None

        if key is None:
            return None

        self._last_seen[key] = now
        if key != self._cand:
            self._cand, self._cand_since = key, now
        if key == self._fired:
            return None
        if now - self._cand_since >= self.stable_s:
            self._fired = key
            return resolution
        return None
