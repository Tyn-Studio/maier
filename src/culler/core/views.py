"""Thin request handlers -- filtering/grouping logic lives in queries.py,
file moves in moves.py, previews in previews.py. See SPEC §10 for the UI
spec these implement.
"""

from django.conf import settings
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import phaseb, previews, queries, streaming
from .models import DuplicatePair, Photo

_DUPE_ACTIONS = {"keep_left", "keep_right", "keep_both", "defer"}

PAGE_SIZE = 200
NEIGHBOUR_WINDOW = 10


def healthz(request):
    return HttpResponse("ok")


def home(request):
    return redirect("grid")


def preview(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    path = previews.preview_path(settings.WORKING_FOLDER, photo)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def stream(request, pk):
    """Range-request streaming of an original video file (SPEC §10). `pk`
    is any Photo row; `?companion=1` serves its paired Live Photo `.mov`
    instead of its own file (the companion has its own, hidden, Photo row
    with its own pk -- but the review template only knows the image's pk,
    so this is simplest for callers: one URL, one query param).
    """
    photo = get_object_or_404(Photo, pk=pk)
    if request.GET.get("companion") == "1":
        if not photo.live_photo_video_path:
            raise Http404("photo has no Live Photo companion")
        rel_path = photo.live_photo_video_path
    else:
        rel_path = photo.relative_path
    return streaming.serve_file_range(request, settings.WORKING_FOLDER / rel_path)


def grid(request):
    filters = request.GET
    photos_qs = queries.filtered_photos(filters)
    paginator = Paginator(photos_qs, PAGE_SIZE)
    page = paginator.get_page(filters.get("page") or 1)

    dupe_counts = phaseb.duplicate_counts()
    page_photos = list(page.object_list)
    for photo in page_photos:
        photo.dupe_count = dupe_counts.get(photo.sha256, 0)
        photo.is_live = bool(photo.live_photo_video_path)

    context = {
        "day_groups": queries.group_by_day(page_photos),
        "dupe_counts": dupe_counts,
        "page": page,
        "querystring": queries.querystring_without_page(filters),
        "provenances": queries.distinct_provenances(),
        "filter_status": filters.get("status", ""),
        "filter_provenance": filters.get("provenance", ""),
        "filter_from": filters.get("from", ""),
        "filter_to": filters.get("to", ""),
        "unresolved_pair_count": phaseb.unresolved_pair_count(),
    }
    template = "_grid_items.html" if request.headers.get("HX-Request") else "grid.html"
    return render(request, template, context)


def review(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    filters = request.GET
    ordered_pks = list(queries.filtered_photos(filters).values_list("pk", flat=True))

    idx = ordered_pks.index(pk) if pk in ordered_pks else None
    prev_id = next_id = None
    filmstrip_pks: list[int] = []
    if idx is not None:
        prev_id = ordered_pks[idx - 1] if idx > 0 else None
        next_id = ordered_pks[idx + 1] if idx + 1 < len(ordered_pks) else None
        window_start = max(0, idx - NEIGHBOUR_WINDOW)
        window_end = idx + NEIGHBOUR_WINDOW + 1
        filmstrip_pks = ordered_pks[window_start:window_end]

    photos_by_pk = {p.pk: p for p in Photo.objects.filter(pk__in=filmstrip_pks)}
    filmstrip = [photos_by_pk[fpk] for fpk in filmstrip_pks if fpk in photos_by_pk]

    dupe_count = phaseb.duplicate_counts().get(photo.sha256, 0) if photo.sha256 else 0

    context = {
        "photo": photo,
        "prev_id": prev_id,
        "next_id": next_id,
        "filmstrip": filmstrip,
        "qs": filters.urlencode(),
        "index": idx,
        "total": len(ordered_pks),
        "dupe_count": dupe_count,
    }
    return render(request, "review.html", context)


def set_status(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    photo = get_object_or_404(Photo, pk=pk)
    new_status = request.POST.get("status", "")
    context_mode = request.POST.get("context", "grid")
    qs = request.POST.get("qs", "")

    try:
        photo = phaseb.apply_status_to_group(settings.WORKING_FOLDER, photo, new_status)
    except ValueError:
        return HttpResponse("invalid status", status=400)
    except FileNotFoundError:
        return HttpResponse("file moved or deleted outside Culler", status=409)

    if context_mode == "review":
        next_id = request.POST.get("next")
        url = reverse("review", args=[next_id]) if next_id else reverse("grid")
        if qs:
            url = f"{url}?{qs}"
        response = HttpResponse(status=200)
        response["HX-Redirect"] = url
        return response

    photo.dupe_count = phaseb.duplicate_counts().get(photo.sha256, 0) if photo.sha256 else 0
    photo.is_live = bool(photo.live_photo_video_path)
    return render(request, "_grid_cell.html", {"photo": photo, "querystring": qs})


def _in_flight_scan_progress():
    """Pragmatic single-module read of scan.py's in-flight ScanProgress.
    Isolated here (per PLAN T5 brief) rather than spread across views --
    scan.py exposes no public accessor for "is a scan currently running".
    """
    from . import scan as scan_module

    progress = scan_module._current_scan
    if progress is not None and not progress.finished:
        return progress
    return None


def scan_status(request):
    from .scan import start_background_scan

    progress = _in_flight_scan_progress()
    if progress is None and not Photo.objects.exists():
        # Nothing indexed yet and nothing running: kick off the first scan
        # so a fresh `culler open` in browser mode (which missed the CLI's
        # own start_background_scan call, e.g. tests hitting the view
        # directly) still gets indexed.
        progress = start_background_scan(settings.WORKING_FOLDER)
    return render(request, "_scan_banner.html", {"progress": progress})


def rescan(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from .scan import start_background_scan

    progress = start_background_scan(settings.WORKING_FOLDER)
    return render(request, "_scan_banner.html", {"progress": progress})


def dupes(request):
    """Near-dupe review screen (SPEC §8): shows one unresolved pair at a
    time. `?after=<pk>` (set by `resolve_pair`'s redirect) advances the
    pk-ordered queue past a given pair -- used for both post-action
    navigation and `defer` (which pushes a pair to the back without
    resolving it).
    """
    after = request.GET.get("after")
    after_pk = int(after) if after and after.isdigit() else None
    pair = phaseb.next_unresolved_pair(after_pk=after_pk)
    context = {"pair": pair, "unresolved_count": phaseb.unresolved_pair_count()}
    return render(request, "dupes.html", context)


def resolve_pair(request, pair_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    pair = get_object_or_404(DuplicatePair, pk=pair_id)
    action = request.POST.get("action", "")
    if action not in _DUPE_ACTIONS:
        return HttpResponse("unknown action", status=400)

    folder = settings.WORKING_FOLDER
    try:
        if action == "keep_left":
            phaseb.apply_status_to_group(folder, pair.photo_a, Photo.STATUS_SELECTED)
            phaseb.apply_status_to_group(folder, pair.photo_b, Photo.STATUS_REJECTED)
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        elif action == "keep_right":
            phaseb.apply_status_to_group(folder, pair.photo_b, Photo.STATUS_SELECTED)
            phaseb.apply_status_to_group(folder, pair.photo_a, Photo.STATUS_REJECTED)
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        elif action == "keep_both":
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        # "defer": no DB change -- the redirect below simply requests the
        # next pair after this one, wrapping around, without resolving it.
    except FileNotFoundError:
        return HttpResponse("file moved or deleted outside Culler", status=409)

    url = f"{reverse('dupes')}?after={pair.pk}"
    response = HttpResponse(status=200)
    response["HX-Redirect"] = url
    return response
