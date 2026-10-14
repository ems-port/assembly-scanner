"""Shared application types."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


def normalize_pn(s: str) -> str:
    """Strip, uppercase, collapse internal whitespace. Keeps '-' and '/'."""
    return re.sub(r"\s+", " ", str(s).strip()).upper()


@dataclass(frozen=True)
class BomLine:
    mouser_pn: str                      # normalized
    mpn: str | None
    designators: tuple[str, ...]        # empty -> extra item, no placement
    description: str | None
    package: str | None
    sheet: str
    row: int
    fitted: bool = True


@dataclass
class Bom:
    lines: list[BomLine]
    source_path: str

    @property
    def by_mouser(self) -> dict[str, list[BomLine]]:
        idx: dict[str, list[BomLine]] = {}
        for ln in self.lines:
            idx.setdefault(ln.mouser_pn, []).append(ln)
        return idx


class Status(str, Enum):
    MATCH = "MATCH"                          # in BOM, has placeable designators
    MATCH_NO_PLACEMENT = "MATCH_NO_PLACEMENT"  # in BOM, no designators / all DNF
    NOT_IN_BOM = "NOT_IN_BOM"                # looks like a Mouser P/N, not in BOM
    NO_CANDIDATE = "NO_CANDIDATE"            # nothing usable decoded


@dataclass(frozen=True)
class Resolution:
    status: Status
    mouser_pn: str | None
    lines: tuple[BomLine, ...] = ()
    designators: tuple[str, ...] = ()        # merged, placeable (fitted) only
    raw_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HighlightResult:
    ok: bool
    highlighted: tuple[str, ...] = ()
    not_on_board: tuple[str, ...] = ()
    error: str | None = None
