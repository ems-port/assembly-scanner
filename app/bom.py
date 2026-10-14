"""BOM import: a manually exported purchasing BOM (XLSX or CSV) -> Bom.

The BOM is always a user-selected file, never pulled from Fusion.

Sheet roles when ``sheet=None``:
  * A sheet with a part-number column and a designator column is a placement
    sheet.
  * A sheet with a part-number column and a quantity column but no designators
    is an extras sheet. Its lines are known but have no placement.
  * A sheet with a part-number column but neither designators nor quantity is
    a reference sheet and is not indexed as a placement.
An explicitly named ``sheet`` is always loaded.

The load report is attached as ``bom.report`` (a LoadReport) because the
shared ``Bom`` type has no slot for it; use ``load_report(bom)`` to read it.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.models import Bom, BomLine, normalize_pn

# --------------------------------------------------------------------------
# Header synonyms (compared after _hnorm: lowercase, alphanumerics only).
# Exact matches only, so e.g. 'Notes / designators (this export)' and
# 'Customer # (cart)' are NOT taken as the designator column.
# --------------------------------------------------------------------------
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "designators": ("designators", "designator", "refdes", "refdess", "reference",
                    "references", "referencedesignator", "referencedesignators",
                    "refs", "ref", "parts", "part"),
    "mpn": ("mpn", "mfrpn", "mfgpn", "manufacturerpn", "manufacturerpartnumber",
            "mfrpartnumber", "mfgpartnumber", "manufacturerpartno", "mfrpartno"),
    "description": ("description", "desc", "item", "value"),
    "package": ("package", "footprint", "case", "pkg", "casepackage"),
    "fitted": ("fitteddnf", "fitted", "dnf", "dnp", "populate", "populated",
               "fit", "fitteddnp", "assemble"),
}
_FIELDS = ("mouser_pn", "designators", "mpn", "description", "package", "fitted", "qty")

# Loose shape for a P/N cell to be accepted at load time: one token, has a '-'
# and a digit. The stricter TBR-1 shape below only produces a warning.
_PN_LOOSE = re.compile(r"^(?=.*\d)\S+-\S+$")
MOUSER_PN_SHAPE = re.compile(r"^\d{2,3}-\S+$")   # TBR-1, shared with resolver
_DESIGNATOR = re.compile(r"^[A-Z][A-Z_$]*\d+[A-Z0-9_]*$")
_RANGE = re.compile(r"^([A-Z][A-Z_$]*)(\d+)-(?:\1)?(\d+)$")
_NOT_FITTED = {"DNF", "DNP", "NO", "N", "FALSE", "0", "NOT FITTED", "NOFIT", "NF", "X"}
_HEADER_SCAN_ROWS = 30


def _hnorm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).lower()) if v is not None else ""


def _field_for_header(h: Any) -> str | None:
    n = _hnorm(h)
    if not n:
        return None
    if n.startswith("mouser") and any(k in n for k in ("pn", "part", "no", "number", "code")) \
            and not any(k in n for k in ("price", "stock", "desc")):
        return "mouser_pn"
    if n.startswith("qty") or n.startswith("quantity"):
        return "qty"
    for f, syns in _SYNONYMS.items():
        if n in syns:
            return f
    return None


# --------------------------------------------------------------------------
# Report types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SkippedRow:
    sheet: str
    row: int
    reason: str
    excerpt: str


@dataclass
class SheetInfo:
    sheet: str
    role: str                    # placement | extras | reference | none
    header_row: int | None
    columns: dict[str, int]      # field -> 0-based column index
    lines: int = 0
    note: str = ""


@dataclass
class LoadReport:
    source_path: str
    sheets: list[SheetInfo] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # P/N -> [(sheet, row), ...] for a P/N on more than one line OF THE SAME SHEET
    duplicate_pns: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    # P/N -> [sheet, ...] for a P/N on several sheets (normal for BOM + Order;
    # worth a look for Basket + Tuning kit). Lines are merged by resolve().
    cross_sheet_pns: dict[str, list[str]] = field(default_factory=dict)
    # designator -> [P/N, ...] for designators claimed by >1 distinct P/N
    designator_conflicts: dict[str, list[str]] = field(default_factory=dict)
    lines_loaded: int = 0
    pn_count: int = 0
    placement_pns: int = 0
    extra_pns: int = 0

    def summary(self) -> str:
        out = [f"BOM {Path(self.source_path).name}: {self.lines_loaded} lines, "
               f"{self.pn_count} P/Ns ({self.placement_pns} with placements, "
               f"{self.extra_pns} extras only)"]
        for s in self.sheets:
            out.append(f"  sheet '{s.sheet}': {s.role}, {s.lines} lines"
                       + (f" ({s.note})" if s.note else ""))
        if self.skipped:
            out.append(f"  {len(self.skipped)} rows skipped:")
            out += [f"    {k.sheet}!{k.row}: {k.reason} [{k.excerpt}]" for k in self.skipped]
        for pn, locs in self.duplicate_pns.items():
            out.append(f"  duplicate P/N {pn}: " + ", ".join(f"{s}!{r}" for s, r in locs))
        if self.cross_sheet_pns:
            out.append(f"  {len(self.cross_sheet_pns)} P/Ns appear on several sheets (merged): "
                       + ", ".join(f"{pn} [{'+'.join(sh)}]" for pn, sh in self.cross_sheet_pns.items()))
        for d, pns in self.designator_conflicts.items():
            out.append(f"  designator {d} on several P/Ns (replacement?): {', '.join(pns)}")
        out += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(out)


def load_report(bom: Bom) -> LoadReport | None:
    return getattr(bom, "report", None)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def parse_designators(cell: Any) -> tuple[tuple[str, ...], list[str]]:
    """'C2,C3, C4' -> (('C2','C3','C4'), []). Also 'R1-R4' / 'R1-4' ranges.
    Returns (designators, invalid_tokens)."""
    if cell is None:
        return (), []
    toks = [t for t in re.split(r"[,;\s]+", str(cell).strip().upper()) if t]
    out: list[str] = []
    bad: list[str] = []
    for t in toks:
        m = _RANGE.match(t)
        if m and int(m.group(2)) <= int(m.group(3)) and int(m.group(3)) - int(m.group(2)) < 500:
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        elif _DESIGNATOR.match(t):
            out.append(t)
        else:
            bad.append(t)
    seen: set[str] = set()
    uniq = tuple(d for d in out if not (d in seen or seen.add(d)))
    return uniq, bad


def _is_fitted(v: Any) -> bool:
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().upper() not in _NOT_FITTED


def _txt(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _excerpt(row: Iterable[Any]) -> str:
    s = " | ".join(str(v).strip() for v in row if v is not None and str(v).strip())
    return s[:80] + ("..." if len(s) > 80 else "")


def _read_sheets(path: Path) -> list[tuple[str, list[list[Any]]]]:
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        text = path.read_text(encoding="utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
        rows = [list(r) for r in csv.reader(text.splitlines(), dialect)]
        return [(path.stem, rows)]
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)])
                for ws in wb.worksheets]
    finally:
        wb.close()


def _resolve_column_map(column_map: dict, header: list[Any]) -> dict[str, int]:
    cols: dict[str, int] = {}
    hn = [_hnorm(h) for h in header]
    for f, spec in column_map.items():
        if f == "header_row":
            continue
        if f not in _FIELDS:
            raise ValueError(f"column_map: unknown field {f!r} (known: {_FIELDS})")
        if isinstance(spec, int):
            cols[f] = spec
        elif isinstance(spec, str) and re.fullmatch(r"[A-Za-z]{1,2}", spec) and _hnorm(spec) not in hn:
            from openpyxl.utils import column_index_from_string
            cols[f] = column_index_from_string(spec.upper()) - 1
        elif _hnorm(spec) in hn:
            cols[f] = hn.index(_hnorm(spec))
    return cols


def _detect_header(rows: list[list[Any]], column_map: dict | None) -> tuple[int | None, dict[str, int]]:
    """Return (0-based header row index, field->col). Header = first row with a
    Mouser P/N column (by synonym, or by the column_map's mouser_pn name)."""
    if column_map and "header_row" in column_map:
        i = int(column_map["header_row"]) - 1
        if 0 <= i < len(rows):
            return i, _auto_cols(rows[i]) | _resolve_column_map(column_map, rows[i])
        return None, {}
    for i, r in enumerate(rows[:_HEADER_SCAN_ROWS]):
        cols = _auto_cols(r)
        if column_map:
            cols |= _resolve_column_map(column_map, r)
        if "mouser_pn" in cols and (not column_map or "mouser_pn" not in column_map
                                    or isinstance(column_map["mouser_pn"], int)
                                    or _hnorm(column_map["mouser_pn"]) in map(_hnorm, r)):
            return i, cols
    return None, {}


def _auto_cols(header: list[Any]) -> dict[str, int]:
    cols: dict[str, int] = {}
    prio = {s: k for k, s in enumerate(_SYNONYMS["description"])}
    for j, h in enumerate(header):
        f = _field_for_header(h)
        if f is None:
            continue
        if f == "description" and "description" in cols:
            # prefer 'Description' over 'Item' over 'Value'
            if prio[_hnorm(h)] < prio[_hnorm(header[cols["description"]])]:
                cols[f] = j
            continue
        cols.setdefault(f, j)
    return cols


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def load_bom(path: str | Path, sheet: str | list[str] | None = None,
             column_map: dict | None = None) -> Bom:
    """Load a purchasing BOM file (XLSX or CSV).

    sheet: None = every sheet with a Mouser P/N column (see module doc for
        placement / extras / reference roles); a name or list of names = only
        those sheets, each loaded whatever its role.
    column_map: optional override, {field: header text | 0-based int | 'E'},
        fields: mouser_pn, designators, mpn, description, package, fitted, qty,
        plus optional 'header_row' (1-based).
    """
    path = Path(path)
    rep = LoadReport(source_path=str(path))
    all_sheets = _read_sheets(path)
    if sheet is not None:
        wanted = [sheet] if isinstance(sheet, str) else list(sheet)
        names = [n for n, _ in all_sheets]
        missing = [w for w in wanted if w not in names]
        if missing:
            raise ValueError(f"sheet(s) {missing} not in {path.name}; have {names}")
        all_sheets = [(n, r) for n, r in all_sheets if n in wanted]
    explicit = sheet is not None

    lines: list[BomLine] = []
    for name, rows in all_sheets:
        hi, cols = _detect_header(rows, column_map)
        if hi is None:
            rep.sheets.append(SheetInfo(name, "none", None, {}, note="no Mouser P/N column"))
            if explicit:
                raise ValueError(f"sheet {name!r}: no Mouser P/N header found in the first "
                                 f"{_HEADER_SCAN_ROWS} rows; pass column_map")
            continue
        if "designators" in cols:
            role = "placement"
        elif "qty" in cols or explicit:
            role = "extras"
        else:
            rep.sheets.append(SheetInfo(
                name, "reference", hi + 1, cols,
                note="Mouser P/N but no designator or qty column: a sourcing reference, "
                     "not an order list; not indexed (pass sheet= to force)"))
            continue
        info = SheetInfo(name, role, hi + 1, cols)
        rep.sheets.append(info)
        lines_before = len(lines)
        for i in range(hi + 1, len(rows)):
            r = rows[i]
            rown = i + 1
            if not any(v is not None and str(v).strip() for v in r):
                continue

            def get(f: str) -> Any:
                j = cols.get(f)
                return r[j] if j is not None and j < len(r) else None

            raw_pn = _txt(get("mouser_pn"))
            ex = _excerpt(r)
            if raw_pn is None or raw_pn in ("-", "—", "–"):
                why = "no Mouser P/N"
                if not _is_fitted(get("fitted")):
                    why = "DNF row with no Mouser P/N"
                rep.skipped.append(SkippedRow(name, rown, why, ex))
                continue
            if raw_pn.upper().startswith("N/A"):
                rep.skipped.append(SkippedRow(name, rown, "P/N marked N/A (not a Mouser part)", ex))
                continue
            pn = normalize_pn(raw_pn)
            if _field_for_header(raw_pn) == "mouser_pn":
                rep.skipped.append(SkippedRow(name, rown, "repeated header row", ex))
                continue
            if not _PN_LOOSE.match(pn):
                rep.skipped.append(SkippedRow(name, rown, f"P/N cell {raw_pn[:30]!r} is not a part number", ex))
                continue
            if not MOUSER_PN_SHAPE.match(pn):
                rep.warnings.append(f"{name}!{rown}: P/N {pn} does not match the Mouser shape "
                                    f"(TBR-1 regex); a scan of it would not be recognised as a P/N "
                                    f"if it were absent, but it IS indexed")
            desigs: tuple[str, ...] = ()
            if role == "placement":
                desigs, bad = parse_designators(get("designators"))
                if bad and not desigs:
                    rep.skipped.append(SkippedRow(name, rown, f"designator cell unparseable: {bad[:3]}", ex))
                    continue
                if bad:
                    rep.warnings.append(f"{name}!{rown}: ignored non-designator tokens {bad}")
                if not desigs:
                    rep.warnings.append(f"{name}!{rown}: {pn} on a placement sheet with no designators")
            lines.append(BomLine(
                mouser_pn=pn,
                mpn=_txt(get("mpn")),
                designators=desigs,
                description=_txt(get("description")),
                package=_txt(get("package")),
                sheet=name,
                row=rown,
                fitted=_is_fitted(get("fitted")),
            ))
        info.lines = len(lines) - lines_before

    bom = Bom(lines=lines, source_path=str(path))
    _analyse(bom, rep)
    bom.report = rep  # type: ignore[attr-defined]
    return bom


def _analyse(bom: Bom, rep: LoadReport) -> None:
    idx = bom.by_mouser
    rep.lines_loaded = len(bom.lines)
    rep.pn_count = len(idx)
    placeable = {pn for pn, ls in idx.items() if any(l.fitted and l.designators for l in ls)}
    rep.placement_pns = len(placeable)
    rep.extra_pns = rep.pn_count - rep.placement_pns
    for pn, ls in idx.items():
        sheets = list(dict.fromkeys(l.sheet for l in ls))
        if len(sheets) > 1:
            rep.cross_sheet_pns[pn] = sheets
        for sh in sheets:
            same = [(l.sheet, l.row) for l in ls if l.sheet == sh]
            if len(same) > 1:
                rep.duplicate_pns.setdefault(pn, []).extend(same)
    owners: dict[str, list[str]] = {}
    for l in bom.lines:
        if not l.fitted:
            continue
        for d in l.designators:
            if l.mouser_pn not in owners.setdefault(d, []):
                owners[d].append(l.mouser_pn)
    rep.designator_conflicts = {d: pns for d, pns in owners.items() if len(pns) > 1}
    for pn, ls in idx.items():
        dn = [l for l in ls if not l.fitted and l.designators]
        if dn and pn in placeable:
            rep.warnings.append(f"{pn}: fitted on one line and DNF on another "
                                f"({', '.join(f'{l.sheet}!{l.row}' for l in dn)})")
