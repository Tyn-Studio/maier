"""View-support helpers for the grid/review screens: filtering, day
grouping, and querystring plumbing. Kept out of views.py per SPEC §10 /
CLAUDE.md ("keep views thin").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from itertools import groupby
from typing import Any

from django.db.models import QuerySet
from django.http import QueryDict

from . import phaseb
from .models import Photo


def filtered_photos(params: QueryDict | dict[str, Any]) -> QuerySet[Photo]:
    """captured_at-ascending queryset, missing excluded, with optional
    status / provenance / capture-date-range filters from a GET querydict.
    Exact-dupe groups (SPEC §8) are collapsed to their representative --
    redundant copies never appear in the grid/filmstrip/navigation order.
    """
    qs = Photo.objects.filter(missing=False).order_by("captured_at", "pk")
    qs = qs.exclude(pk__in=phaseb.non_representative_pks())

    status = params.get("status")
    if status:
        qs = qs.filter(status=status)

    provenance = params.get("provenance")
    if provenance:
        qs = qs.filter(provenance=provenance)

    from_date = _parse_date(params.get("from"))
    if from_date is not None:
        qs = qs.filter(captured_at__gte=_day_start(from_date))

    to_date = _parse_date(params.get("to"))
    if to_date is not None:
        qs = qs.filter(captured_at__lte=_day_end(to_date))

    return qs


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=UTC)


def group_by_day(photos: list[Photo]) -> list[dict[str, Any]]:
    """Group an already captured_at-ordered list into day buckets with a
    human day header ("Sat 14 Jun 2025"). Only groups adjacent items -- fine
    since callers always pass a single, already-sorted page.
    """
    groups: list[dict[str, Any]] = []
    for day, items in groupby(photos, key=lambda p: p.captured_at.date()):
        groups.append({"day": _format_day(day), "photos": list(items)})
    return groups


def _format_day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}"


def distinct_provenances() -> list[str]:
    return list(
        Photo.objects.exclude(provenance="")
        .order_by("provenance")
        .values_list("provenance", flat=True)
        .distinct()
    )


def querystring_without_page(params: QueryDict) -> str:
    qd = params.copy()
    qd.pop("page", None)
    return qd.urlencode()
