"""Small, data-independent checks for public core behavior."""

from app.hud import hud_sections
from app.models import Bom, BomLine, Status
from app.resolver import resolve


def _line(designators=()):
    return BomLine(
        mouser_pn="123-EXAMPLE",
        mpn="EXAMPLE-1",
        designators=tuple(designators),
        description="Example component",
        package="0402",
        sheet="Assembly",
        row=2,
    )


def test_resolve_returns_fitted_designators_from_bom():
    result = resolve(["123-EXAMPLE"], Bom([_line(("R1", "R2"))], "sample.xlsx"))

    assert result.status is Status.MATCH
    assert result.designators == ("R1", "R2")


def test_resolve_reports_known_part_without_placement():
    result = resolve(["123-EXAMPLE"], Bom([_line()], "sample.xlsx"))

    assert result.status is Status.MATCH_NO_PLACEMENT
    assert result.designators == ()


def test_hud_appends_sum_for_more_than_three_designators():
    result = resolve(
        ["123-EXAMPLE"],
        Bom([_line(("C1", "C2", "C3", "C4"))], "sample.xlsx"),
    )
    _, rows = hud_sections(result, ctx={"bom_name": "sample.xlsx"})

    assert any(text == "C1 C2 C3 C4  sum: 4" for text, _, _ in rows)


def test_hud_does_not_append_sum_for_three_or_fewer_designators():
    result = resolve(
        ["123-EXAMPLE"], Bom([_line(("C1", "C2", "C3"))], "sample.xlsx")
    )
    _, rows = hud_sections(result, ctx={"bom_name": "sample.xlsx"})

    assert any(text == "C1 C2 C3" for text, _, _ in rows)
