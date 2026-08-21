"""View-support helpers for the grid/review screens: filtering, day
grouping, and querystring plumbing. Kept out of views.py per SPEC §10 /
CLAUDE.md ("keep views thin").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from itertools import groupby
from typing import Any

from django.db.models import Count, QuerySet, Sum
from django.http import QueryDict

from . import phaseb
from .models import Photo


def filtered_photos(params: QueryDict | dict[str, Any]) -> QuerySet[Photo]:
    """captured_at-ascending queryset, with optional status / provenance /
    capture-date-range filters from a GET querydict.

    Missing photos (SPEC §6 "hidden by default, state retained") are
    excluded unless `?show=missing`, which flips to *only* missing photos --
    the dedicated missing-file review list. Exact-dupe/Live-Photo-companion
    collapsing (SPEC §8/§6.4) only applies to the normal, non-missing view:
    both helpers already scope to `missing=False` photos, so they'd never
    match anything in the missing view anyway.
    """
    show_missing = params.get("show") == "missing"
    qs = Photo.objects.filter(missing=show_missing).order_by("captured_at", "pk")
    if not show_missing:
        qs = qs.exclude(pk__in=phaseb.non_representative_pks())
        qs = qs.exclude(relative_path__in=phaseb.live_photo_companion_paths())

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

    if params.get("dates") == "low":
        qs = qs.exclude(captured_at_source="exif")

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


def missing_photo_count() -> int:
    return Photo.objects.filter(missing=True).count()


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


def total_photo_count() -> int:
    """Unfiltered row count, used by the grid to distinguish "genuinely
    empty folder" / "still indexing" from "filters matched nothing" (T13
    empty-state item).
    """
    return Photo.objects.count()


# --- Summary screen (SPEC §10) ---------------------------------------------


def counts_by_status() -> dict[str, int]:
    """All photos (missing included -- the summary is an audit view), one
    key per status choice so callers never need `.get(..., 0)`.
    """
    qs = Photo.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    rows = dict(qs)
    return {choice: rows.get(choice, 0) for choice, _label in Photo.STATUS_CHOICES}


def counts_by_provenance_status() -> list[dict[str, Any]]:
    """Provenance x status matrix, one row per provenance (root files under
    the empty-string provenance shown as "(root)"), sorted by provenance
    name.
    """
    rows = Photo.objects.values("provenance", "status").annotate(n=Count("id"))
    table: dict[str, dict[str, int]] = {}
    for row in rows:
        table.setdefault(row["provenance"], {})[row["status"]] = row["n"]

    result = []
    for provenance in sorted(table):
        counts = table[provenance]
        by_status = {choice: counts.get(choice, 0) for choice, _label in Photo.STATUS_CHOICES}
        result.append(
            {
                "provenance": provenance or "(root)",
                **by_status,
                "total": sum(by_status.values()),
            }
        )
    return result


def selected_size_bytes() -> int:
    total = Photo.objects.filter(status=Photo.STATUS_SELECTED).aggregate(total=Sum("file_size"))[
        "total"
    ]
    return total or 0


def human_size(num_bytes: int) -> str:
    """Binary (1024-based) GB/MB/KB formatting for the summary screen."""
    size = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{int(size)} bytes"  # unreachable, satisfies static analysis


def recent_activity(limit: int = 10) -> list[Photo]:
    """The `limit` most recently status-changed photos, newest first."""
    return list(
        Photo.objects.exclude(status_changed_at__isnull=True).order_by("-status_changed_at")[:limit]
    )
