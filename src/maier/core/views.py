"""Thin request handlers -- filtering/grouping logic lives in queries.py,
file moves in moves.py, previews in previews.py. See SPEC §10 for the UI
spec these implement.
"""

import calendar
from datetime import date
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import (
    culling,
    disconnect,
    downloads,
    export,
    folder_settings,
    phaseb,
    preview_upgrade,
    previews,
    pull,
    queries,
    remote_state,
    streaming,
)
from .icloud import ICloudClient, ICloudError, TwoFactorRequired
from .models import DuplicatePair, Photo

_DUPE_ACTIONS = {"keep_left", "keep_right", "keep_both", "defer"}

# Module-level pending-2FA store: single-user localhost app (SPEC/PLAN M5),
# so an in-process dict keyed by email is fine -- there is exactly one
# server, one browser, one person driving it. Flagged per T18 brief: a
# multi-worker/multi-process deployment would need this in the DB or a
# shared cache instead. Never holds a password, only the already-constructed
# pending `ICloudClient` (its underlying pyicloud session already completed
# the password exchange with Apple).
_pending_2fa: dict[str, ICloudClient] = {}

PAGE_SIZE = 200
NEIGHBOUR_WINDOW = 10

# T22: filmstrip neighbours prefetched alongside the current photo's own
# thumb->medium upgrade -- deliberately much narrower than NEIGHBOUR_WINDOW
# (±10, for the visible filmstrip thumbnails) since each one costs ~1MB.
SHARP_PREFETCH_RADIUS = 3
SHARP_MAX_TRIES = 15  # ~12s of polling (load delay:800ms) before giving up

# T34: grid-cell thumb self-heal poller (`_cell_thumb.html`/`cell_thumb`).
CELL_THUMB_MAX_TRIES = 20  # ~40s of polling (load delay:2s) before giving up


def healthz(request):
    return HttpResponse("ok")


def home(request):
    return redirect("grid")


def preview(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    path = previews.preview_path(settings.WORKING_FOLDER, photo)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    if path.name == previews._PLACEHOLDER_NAME:
        # A placeholder is a *pending* preview (remote photo whose medium
        # hasn't been fetched yet, RAW before exiftool, etc.) -- it must
        # never be cached, or the browser pins gray squares forever even
        # after the real preview lands (found live, 2026-08-24).
        response["Cache-Control"] = "no-store"
    else:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def preview_sharp(request, pk):
    """T22 review-screen quality upgrade: remote rows serve the best cached
    tier (medium if it landed, else the bulk thumb, else the placeholder --
    `previews.best_remote_preview`, never a network fetch from this request
    path); local rows just serve the ordinary `preview_path` result. Cache
    headers mirror `preview()`'s no-store-for-placeholder rule, extended to
    "no-store for anything short of medium" for remote rows -- otherwise the
    browser would pin the soft thumb (or gray placeholder) forever the first
    time this URL is hit, before the medium has had a chance to land.
    """
    photo = get_object_or_404(Photo, pk=pk)
    folder = settings.WORKING_FOLDER

    if photo.source != Photo.SOURCE_ICLOUD:
        path = previews.preview_path(folder, photo)
        response = FileResponse(path.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    path = previews.best_remote_preview(folder, photo)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    if path.name == previews._PLACEHOLDER_NAME:
        # Placeholder = pending; must never stick in a cache.
        response["Cache-Control"] = "no-store"
    else:
        # Thumb AND medium are immutable now that callers rev the URL
        # (?v=t / ?v=m, flipping when the medium lands, 2026-08-25) --
        # the un-cached thumb refetched on every 800ms poll/page nav was
        # the review screen's big-image flicker (CTO report).
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
    folder = settings.WORKING_FOLDER
    current_settings = folder_settings.load_settings(folder)
    wrange = folder_settings.working_range(current_settings)
    if wrange is None:
        # T29: never gate any other page -- the setup wizard itself links
        # out to /accounts, /settings, /healthz, etc.
        return redirect("setup")

    filters = request.GET
    # T29: absent `from`/`to` params default to the working range; an
    # explicit param (including an explicitly-cleared empty one) always
    # wins. `QueryDict.copy()` is mutable, unlike `request.GET` itself.
    effective_filters = filters.copy()
    range_from, range_to = wrange
    if "from" not in filters and range_from is not None:
        effective_filters["from"] = range_from.isoformat()
    if "to" not in filters and range_to is not None:
        effective_filters["to"] = range_to.isoformat()

    photos_qs = queries.filtered_photos(effective_filters)
    paginator = Paginator(photos_qs, PAGE_SIZE)
    page = paginator.get_page(filters.get("page") or 1)

    dupe_counts = phaseb.duplicate_counts()
    page_photos = list(page.object_list)
    for photo in page_photos:
        photo.dupe_count = dupe_counts.get(photo.sha256, 0)
        photo.is_live = bool(photo.live_photo_video_path)
        photo.download_pending = _download_pending(photo)
        photo.thumb_pending = _enqueue_missing_thumb(folder, photo)
        _annotate_preview_v(folder, photo)

    scan_progress = _in_flight_scan_progress()

    context = {
        "day_groups": queries.group_by_day(page_photos),
        "dupe_counts": dupe_counts,
        "page": page,
        "querystring": queries.querystring_without_page(filters),
        "provenances": queries.distinct_provenances(),
        "filter_status": filters.get("status", ""),
        "filter_provenance": filters.get("provenance", ""),
        "filter_from": effective_filters.get("from", ""),
        "filter_to": effective_filters.get("to", ""),
        "filter_dates_low": filters.get("dates") == "low",
        "missing_count": queries.missing_photo_count(),
        "show_missing": filters.get("show") == "missing",
        "total_photo_count": queries.total_photo_count(),
        "scanning": scan_progress is not None,
        "scan_progress": scan_progress,
    }
    template = "_grid_items.html" if request.headers.get("HX-Request") else "grid.html"
    return render(request, template, context)


def review(request, pk):
    photo = get_object_or_404(Photo, pk=pk)
    folder = settings.WORKING_FOLDER
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
    for fphoto in filmstrip:
        fphoto.download_pending = _download_pending(fphoto)
        _annotate_preview_v(folder, fphoto)

    dupe_count = phaseb.duplicate_counts().get(photo.sha256, 0) if photo.sha256 else 0
    photo.download_pending = _download_pending(photo)
    _annotate_preview_v(folder, photo)
    photo.sharp_v = _sharp_v(folder, photo)

    # Seamless arrow-nav (CTO, 2026-08-25): preload the prev/next photos'
    # main review images so navigation paints from browser cache instead of
    # fetching after the click.
    preload_urls = []
    for npk in (prev_id, next_id):
        if npk is None:
            continue
        nphoto = photos_by_pk.get(npk)
        if nphoto is None:
            continue
        v = _sharp_v(folder, nphoto)
        if v != "0":
            preload_urls.append(f"{reverse('preview-sharp', args=[npk])}?v={v}")

    context = {
        "photo": photo,
        "prev_id": prev_id,
        "next_id": next_id,
        "filmstrip": filmstrip,
        "qs": filters.urlencode(),
        "index": idx,
        "total": len(ordered_pks),
        "dupe_count": dupe_count,
        "filesize_display": queries.human_size(photo.file_size),
        "preload_urls": preload_urls,
        **_sharp_preview_context(settings.WORKING_FOLDER, photo, ordered_pks, idx),
    }
    return render(request, "review.html", context)


def _sharp_preview_context(folder, photo: Photo, ordered_pks: list[int], idx: int | None) -> dict:
    """T22: for a remote (non-video) photo, kick off its own thumb->medium
    upgrade plus its nearest filmstrip neighbours' (they're the next photos
    the user is likely to land on), and hand `review.html` what it needs to
    render the initial poller partial. Local photos and remote videos get an
    empty dict -- `review.html` never includes the poller partial for them.
    """
    if photo.source != Photo.SOURCE_ICLOUD or photo.media_type == Photo.MEDIA_VIDEO:
        return {}

    preview_upgrade.enqueue_medium(folder, photo)

    if idx is not None:
        neighbour_pks: list[int] = []
        for offset in range(1, SHARP_PREFETCH_RADIUS + 1):
            if idx - offset >= 0:
                neighbour_pks.append(ordered_pks[idx - offset])
            if idx + offset < len(ordered_pks):
                neighbour_pks.append(ordered_pks[idx + offset])
        for neighbour in Photo.objects.filter(pk__in=neighbour_pks, source=Photo.SOURCE_ICLOUD):
            preview_upgrade.enqueue_medium(folder, neighbour)

    medium_dest = previews.remote_medium_dest(folder, photo.account, photo.remote_id or "")
    return {"medium_ready": medium_dest.exists(), "tries": 0, "max_tries": SHARP_MAX_TRIES}


def sharp_status(request, pk):
    """One step of the review image's recursive load-polling
    (`_review_sharp.html`, mirrors `scan_status`/`_scan_banner.html`): a
    medium that has landed ends the chain with the sharp `<img>`; otherwise
    the same poller re-renders, up to `SHARP_MAX_TRIES` (~12s) before giving
    up and leaving the thumb. Re-issues `enqueue_medium` on every poll
    (cheap no-op once cached or already pending) so a worker restart mid-
    poll self-heals without waiting for the photo to be reopened.
    """
    photo = get_object_or_404(Photo, pk=pk)
    folder = settings.WORKING_FOLDER
    tries = int(request.GET.get("tries") or 0)

    medium_dest = previews.remote_medium_dest(folder, photo.account, photo.remote_id or "")
    medium_ready = medium_dest.exists()
    if not medium_ready:
        preview_upgrade.enqueue_medium(folder, photo)

    context = {
        "photo": photo,
        "medium_ready": medium_ready,
        "tries": tries,
        "max_tries": SHARP_MAX_TRIES,
    }
    return render(request, "_review_sharp.html", context)


def _enqueue_missing_thumb(folder, photo: Photo) -> bool:
    """T34: for a visible-page remote row whose bulk-synced thumb hasn't
    landed yet, kick off an on-demand fetch (`preview_upgrade.enqueue_thumb`)
    instead of waiting for the whole-library backfill to reach it in its own
    order. One `Path.exists()` stat per remote row on the page (~µs) --
    cheap even at `PAGE_SIZE` (200). Local rows and remote rows that already
    have a cached thumb are untouched (no stat needed for local rows,
    `False` returned immediately) so the grid's fast path never gains an
    extra request. The return value drives `_grid_cell.html`'s poller.
    """
    if photo.source != Photo.SOURCE_ICLOUD or not photo.remote_id:
        return False
    dest = previews.remote_preview_dest(folder, photo.account, photo.remote_id)
    if dest.exists():
        return False
    preview_upgrade.enqueue_thumb(folder, photo)
    return True


def cell_thumb(request, pk):
    """One step of a grid cell's thumbnail self-heal poll (T34, mirrors
    `sharp_status`/`_review_sharp.html`'s recursive load-polling idiom): a
    thumb that has landed ends the chain with the plain `<img>` markup
    (identical to the cached-thumb fast path -- `_cell_thumb.html`); else
    the poller re-renders with `tries+1`, capped at `CELL_THUMB_MAX_TRIES`
    (~40s at `load delay:2s`) before giving up and leaving the placeholder
    image (the whole-library backfill will still reach it eventually).
    Re-issues `enqueue_thumb` on every poll (cheap no-op once cached or
    already pending) so a worker restart mid-poll self-heals without
    needing the grid page to be reloaded.
    """
    photo = get_object_or_404(Photo, pk=pk)
    folder = settings.WORKING_FOLDER
    tries = int(request.GET.get("tries") or 0)

    dest = previews.remote_preview_dest(folder, photo.account, photo.remote_id or "")
    ready = dest.exists()
    if not ready:
        preview_upgrade.enqueue_thumb(folder, photo)

    context = {
        "photo": photo,
        "ready": ready,
        "tries": tries,
        "max_tries": CELL_THUMB_MAX_TRIES,
    }
    return render(request, "_cell_thumb.html", context)


def set_status(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    photo = get_object_or_404(Photo, pk=pk)
    new_status = request.POST.get("status", "")
    context_mode = request.POST.get("context", "grid")
    qs = request.POST.get("qs", "")

    try:
        photo = culling.apply_status_any(settings.WORKING_FOLDER, photo, new_status)
    except ValueError:
        return HttpResponse("invalid status", status=400)
    except FileNotFoundError:
        return HttpResponse("file moved or deleted outside Maier", status=409)
    except culling.AccountSessionExpired as exc:
        # Not currently raised (see culling.py's docstring) -- kept so a
        # future synchronous session check has a status code to land on.
        return HttpResponse(f"iCloud session expired for {exc.account}", status=409)

    if context_mode == "review":
        # PLAN T23 item 3: no auto-advance in review. The user stays on the
        # photo (it may have physically moved on disk, or be download-
        # pending for a remote select) -- only the status pill in the top
        # bar updates; arrows are the only navigation. The partial needs
        # nothing but the photo object.
        return render(request, "_review_status.html", {"photo": photo})

    photo.dupe_count = phaseb.duplicate_counts().get(photo.sha256, 0) if photo.sha256 else 0
    photo.is_live = bool(photo.live_photo_video_path)
    photo.download_pending = _download_pending(photo)
    return render(request, "_grid_cell.html", {"photo": photo, "querystring": qs})


def _sharp_v(folder, photo: Photo) -> str:
    """URL rev for /preview-sharp: 'm' (medium cached), 't' (thumb only),
    '0' (placeholder). Flips the cache key exactly when the served content
    changes tier, letting every non-placeholder response be immutable.
    """
    if photo.source != Photo.SOURCE_ICLOUD or not photo.remote_id:
        return "1"  # local rows: preview_path result, always immutable
    if previews.remote_medium_dest(folder, photo.account, photo.remote_id).exists():
        return "m"
    if previews.remote_preview_dest(folder, photo.account, photo.remote_id).exists():
        return "t"
    return "0"


def _annotate_preview_v(folder, photo: Photo) -> None:
    """Cache-busting rev for remote rows' /preview/<pk> URLs. v0.1.0/0.1.1
    served PLACEHOLDERS with immutable 1-year caching -- browsers from that
    era have gray squares pinned for the bare URL forever (live finding,
    2026-08-25: thumbs on disk + correct serving, grid still gray). The rev
    flips 0 -> 1 when the real thumb exists, changing the cache key exactly
    once; the poisoned bare-URL entry is simply never consulted again.
    Reuses the thumb_pending stat when the caller already computed it.
    """
    if photo.source != Photo.SOURCE_ICLOUD or not photo.remote_id:
        photo.preview_v = ""
        return
    pending = getattr(photo, "thumb_pending", None)
    if pending is None:
        pending = not previews.remote_preview_dest(folder, photo.account, photo.remote_id).exists()
    photo.preview_v = "0" if pending else "1"


def _download_pending(photo: Photo) -> bool:
    """SPEC §18: a selected remote photo whose original hasn't landed yet
    (still `source="icloud"`) -- once the download worker converts the row
    to `source="local"` this is cheaply false, no per-photo state-file read
    needed (PLAN T17 brief: "cheap; no per-photo state reads").
    """
    return photo.source == Photo.SOURCE_ICLOUD and photo.status == Photo.STATUS_SELECTED


def _pull_progress_for(account: str):
    """Isolated single-module read of pull.py's in-flight/last-finished
    `PullProgress` for one account (module dict `pull._current_pulls`) --
    same seam pattern as `_in_flight_scan_progress` above (PLAN T18 brief
    flags both as isolated-helper reads rather than a public accessor on
    pull.py, which exposes none).
    """
    return pull._current_pulls.get(account)


def _recent_download_errors() -> list[str]:
    """Isolated single-module read of downloads.py's `_last_progress`
    (a global, NOT per-account -- downloads.py runs one worker for every
    account's pending items together) -- surfaces the most recent original-
    download run's errors (e.g. an expired session) on the accounts screen
    without polling. Same seam pattern as `_in_flight_scan_progress`/
    `_pull_progress_for` (PLAN T18 brief flags this as the third such seam).
    """
    progress = downloads._last_progress
    return list(progress.errors) if progress is not None else []


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
    """One step of the banner's recursive load-polling (`_scan_banner.html`):
    an in-flight scan renders a live poller div whose "load delay:2s"
    trigger schedules the next request; an idle scan renders an inert div
    and the chain ends -- no stop-polling status code needed.
    """
    from .scan import start_background_scan

    progress = _in_flight_scan_progress()
    if progress is None and not Photo.objects.exists():
        # Nothing indexed yet and nothing running: kick off the first scan
        # so a fresh `maier open` in browser mode (which missed the CLI's
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


def summary(request):
    """Folder audit screen (SPEC §10): counts by status/provenance, total
    `selected/` size, unresolved-dupe/missing counts, recent activity.
    """
    context = {
        "counts_by_status": queries.counts_by_status(),
        "provenance_rows": queries.counts_by_provenance_status(),
        "selected_size": queries.human_size(queries.selected_size_bytes()),
        "unresolved_pair_count": phaseb.unresolved_pair_count(),
        "missing_count": queries.missing_photo_count(),
        "recent_activity": queries.recent_activity(),
    }
    return render(request, "summary.html", context)


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
            culling.apply_status_any(folder, pair.photo_a, Photo.STATUS_SELECTED)
            culling.apply_status_any(folder, pair.photo_b, Photo.STATUS_REJECTED)
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        elif action == "keep_right":
            culling.apply_status_any(folder, pair.photo_b, Photo.STATUS_SELECTED)
            culling.apply_status_any(folder, pair.photo_a, Photo.STATUS_REJECTED)
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        elif action == "keep_both":
            pair.resolved = True
            pair.save(update_fields=["resolved"])
        # "defer": no DB change -- the redirect below simply requests the
        # next pair after this one, wrapping around, without resolving it.
    except FileNotFoundError:
        return HttpResponse("file moved or deleted outside Maier", status=409)
    except culling.AccountSessionExpired as exc:
        return HttpResponse(f"iCloud session expired for {exc.account}", status=409)

    url = f"{reverse('dupes')}?after={pair.pk}"
    response = HttpResponse(status=200)
    response["HX-Redirect"] = url
    return response


# --- iCloud accounts screen (SPEC §18, PLAN T18) ----------------------------


def _accounts_rows(folder) -> list[dict]:
    """One row per attached account (from state files on disk -- never a
    network call, per T18 brief: session status is only ever discovered
    when the user acts, not while just rendering this list).
    """
    rows = []
    for email in remote_state.list_accounts(folder):
        state = remote_state.load_state(folder, email)
        rows.append(
            {
                "email": email,
                "slug": remote_state.account_slug(email),
                "last_pulled": state.cursor,
                # "Remote rows in DB per account: total" (T18 brief) -- kept
                # even after a row converts source="local" on download, since
                # `account`/`remote_id` are left in place by
                # downloads._convert_to_local, so this stays a stable "items
                # known from this account" count across that transition.
                "total": Photo.objects.filter(account=email).count(),
                "pending": len(downloads.pending_remote_ids(email)),
                "pull_progress": _pull_progress_for(email),
            }
        )
    return rows


def _accounts_context(request, next_page: str = "settings", **extra) -> dict:
    """`next_page` is "settings" or "setup" -- which page hosts the shared
    `_accounts_section.html` partial for this render (PLAN T30: the
    accounts screen was merged into Settings, and the setup wizard embeds
    the same section inline for step 1). `accounts_owner_url` /
    `accounts_next` let the partial's confirm/cancel links and the
    connect/2FA forms' hidden `next` field build off whichever page is
    actually hosting them, so a submit from either page lands back on that
    same page.
    """
    owner_url = reverse("setup") if next_page == "setup" else reverse("settings")
    context = {
        "accounts": _accounts_rows(settings.WORKING_FOLDER),
        "download_errors": _recent_download_errors(),
        "added": request.GET.get("added", ""),
        # T21 two-step confirm: `?confirm=<email>` re-renders the host page
        # with an inline confirmation block for that one row instead of a
        # JS confirm() dialog (CLAUDE.md hard rule 4).
        "confirm_disconnect": request.GET.get("confirm", ""),
        "disconnected": request.GET.get("disconnected", ""),
        "accounts_next": next_page,
        "accounts_owner_url": owner_url,
    }
    context.update(extra)
    return context


def _settings_context(request) -> dict:
    folder = settings.WORKING_FOLDER
    return {
        "export_settings": folder_settings.load_settings(folder),
        "notice": request.GET.get("notice", ""),
        "export_progress": _current_export_progress(),
    }


def _render_owner_page(request, next_page: str, **accounts_extra):
    """Re-renders whichever page hosts the accounts section (settings.html
    normally, setup.html step 1 during onboarding) with an accounts-flow
    error/pending state -- used by the login/2FA/pull/disconnect views
    below instead of a redirect when they need to show something instead of
    just moving on (PLAN T30: both accounts.html and the standalone
    settings screen were merged/removed, so there is no more dedicated
    accounts.html to render here).
    """
    accounts_ctx = _accounts_context(request, next_page=next_page, **accounts_extra)
    if next_page == "setup":
        folder = settings.WORKING_FOLDER
        has_accounts = bool(remote_state.list_accounts(folder))
        current = folder_settings.load_settings(folder)
        context = {
            "show_step1": not has_accounts,
            "has_accounts": has_accounts,
            "working_from": current.working_from,
            "working_to": current.working_to,
            **accounts_ctx,
        }
        return render(request, "setup.html", context)

    context = _settings_context(request)
    context.update(accounts_ctx)
    return render(request, "settings.html", context)


def accounts(request):
    """PLAN T30: `/accounts` is now a redirect to the merged Settings page
    (URL name kept working since templates/bookmarks may still use it) --
    any querystring (e.g. a `?confirm=` two-step-confirm link) carries over
    so old links keep working.
    """
    url = reverse("settings")
    query = request.GET.urlencode()
    if query:
        url = f"{url}?{query}"
    return redirect(url)


def account_disconnect(request):
    """ "Disconnect" (SPEC §18 UI, PLAN T21, merged into Settings by T30):
    removes the account's saved session + its remote DB rows + cached
    previews. Keeps `icloud-state/{slug}.json` (durable decisions --
    re-attaching restores rejections) and everything already in
    `selected/` (ordinary local files by then). Two-step confirm lives
    entirely in `_accounts_section.html` (GET `?confirm=<email>` on its
    host page renders the inline confirm block below); this view is only
    the POST that actually performs it. Disconnect is only ever reached
    from Settings (the setup wizard's embedded section has no existing
    accounts to disconnect yet), so errors always re-render there.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("account", "").strip()

    try:
        disconnect.disconnect_account(folder, email)
    except disconnect.PullInFlight as exc:
        return _render_owner_page(request, "settings", disconnect_error=str(exc))

    return redirect(f"{reverse('settings')}?disconnected={email}")


def account_login(request):
    """Add-account form target (SPEC §18 UI). The password lives only in
    `request.POST` for the duration of this request -- it is passed
    straight to `ICloudClient.login` and never assigned to any attribute,
    logged, or written to disk (CLAUDE.md hard rule 8). `next` (PLAN T30)
    is "settings" (default) or "setup" -- which page's embedded accounts
    section submitted this form, so success/pending/error all land back on
    the same one.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "")
    next_page = request.POST.get("next", "settings")
    owner_url = reverse("setup") if next_page == "setup" else reverse("settings")

    try:
        logged_in = ICloudClient.login(email, password)
    except TwoFactorRequired as exc:
        # Single-user localhost app (see `_pending_2fa` module docstring):
        # stash the already-authenticated-pending client so the 2FA POST
        # below submits the code on the SAME pyicloud session.
        _pending_2fa[email] = exc.client
        return _render_owner_page(request, next_page, pending_2fa_email=email)
    except ICloudError as exc:
        return _render_owner_page(request, next_page, login_error=str(exc), prefill_email=email)

    # No 2FA required: ensure a state file exists so the account shows up
    # in list_accounts() even before its first pull.
    remote_state.save_state(folder, remote_state.load_state(folder, email))
    # Auto-start the first pull: a freshly attached account with no photos
    # is a UX trap (authenticated-but-empty timeline, 2026-08-24 incident).
    pull.start_background_pull(folder, logged_in)
    return redirect(f"{owner_url}?added={email}")


def account_2fa(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("email", "").strip()
    code = request.POST.get("code", "")
    next_page = request.POST.get("next", "settings")
    owner_url = reverse("setup") if next_page == "setup" else reverse("settings")

    client = _pending_2fa.pop(email, None)
    if client is None:
        return _render_owner_page(
            request,
            next_page,
            login_error=f"No pending login for {email} -- please log in again below.",
        )

    try:
        verified = client.submit_2fa(code)
    except ICloudError as exc:
        _pending_2fa[email] = client  # keep the pending session for a retry
        return _render_owner_page(request, next_page, pending_2fa_email=email, twofa_error=str(exc))

    if not verified:
        _pending_2fa[email] = client  # keep the pending session for a retry
        return _render_owner_page(
            request, next_page, pending_2fa_email=email, twofa_error="Incorrect verification code."
        )

    remote_state.save_state(folder, remote_state.load_state(folder, email))
    # Auto-start the first pull on the just-verified client (same UX-trap
    # fix as account_login's success path).
    pull.start_background_pull(folder, client)
    return redirect(f"{owner_url}?added={email}")


def account_pull(request):
    """ "Pull now" (SPEC §18 UI). `account` is a POST field rather than a
    path segment (flagged, PLAN T18 brief: avoids mapping an arbitrary
    Apple-ID email on/off a URL-safe slug for routing purposes -- the slug
    is still used for filenames/dirs elsewhere via `remote_state.account_slug`).
    Only ever reachable from the Settings accounts table (the setup
    wizard's embedded section has no existing accounts to pull yet), so
    this always targets Settings.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("account", "").strip()

    client = ICloudClient.from_session(email)
    if client is None:
        return _render_owner_page(
            request,
            "settings",
            pull_error=f"iCloud session for {email} has expired -- please re-authenticate below.",
            prefill_email=email,
        )

    pull.start_background_pull(folder, client)
    # Resumes any previously-stuck selected-pending downloads too (e.g. rows
    # left behind by a session expiry on an earlier worker run).
    downloads.start_worker(folder)
    return redirect("settings")


def pull_status(request):
    """One step of the per-account pull banner's recursive load-polling --
    mirrors `scan_status`/`_scan_banner.html` (T13 decisions log), keyed by
    `?account=` instead of being global.
    """
    account = request.GET.get("account", "")
    context = {
        "account": account,
        "slug": remote_state.account_slug(account) if account else "",
        "progress": _pull_progress_for(account),
    }
    return render(request, "_pull_progress.html", context)


# --- Export (SPEC §3, PLAN T25) --------------------------------------------


def _current_export_progress():
    """Isolated single-module read of export.py's in-flight/last-finished
    `ExportProgress` -- same seam pattern as `_in_flight_scan_progress`/
    `_pull_progress_for` above.
    """
    return export._current_export


def _kick_pulls_for_accounts_with_session(folder) -> None:
    """Starts a background pull for every attached account that still has a
    live iCloud session (PLAN T29/T31): shared by `setup_dates` (unconditional,
    original behaviour) and `settings_page`'s auto-save POST (only when the
    working range actually changed, see `_apply_partial_settings`) so the
    "changing the range should immediately go fetch newly-included backlog"
    intent isn't duplicated between the two. Expired/never-attached sessions
    are skipped silently -- a background nicety, not a hard requirement.
    """
    for email in remote_state.list_accounts(folder):
        client = ICloudClient.from_session(email)
        if client is not None:
            pull.start_background_pull(folder, client)


def _apply_partial_settings(folder, post) -> tuple[folder_settings.FolderSettings, bool]:
    """Applies only the fields present in `post` onto the currently saved
    settings and persists the result (PLAN T31: settings.html auto-saves each
    section independently on change, so a single POST only ever carries one
    section's fields -- the other section's saved values must survive
    untouched, which is the "wipe bug" T30 fixed for export-vs-range and this
    must keep fixed now that both directions go through the same partial
    update).

    The date-prefix checkbox needs its own presence marker
    (`export_date_prefix_submitted`, a hidden input in that form) since an
    *unchecked* checkbox is simply absent from `POST` -- indistinguishable
    from "a different section's form was submitted" without it.

    Returns `(saved, range_changed)`; `range_changed` is True only when
    `working_from`/`working_to` were part of this POST and actually differ
    from what was saved before, used to decide whether to kick background
    pulls (mirrors `setup_dates`, PLAN T29 -- see `_kick_pulls_for_accounts_with_session`).
    """
    current = folder_settings.load_settings(folder)
    range_changed = False

    if "export_destination" in post:
        current.export_destination = post.get("export_destination", "").strip()

    if "export_mode" in post:
        mode = post.get("export_mode", folder_settings.MODE_MANUAL)
        if mode not in (folder_settings.MODE_MANUAL, folder_settings.MODE_AUTOMATIC):
            mode = folder_settings.MODE_MANUAL
        current.export_mode = mode

    if "export_date_prefix_submitted" in post:
        current.export_date_prefix = post.get("export_date_prefix") == "on"

    if "working_from" in post or "working_to" in post:
        new_from = post.get("working_from", current.working_from).strip()
        new_to = post.get("working_to", current.working_to).strip()
        range_changed = new_from != current.working_from or new_to != current.working_to
        current.working_from = new_from
        current.working_to = new_to

    folder_settings.save_settings(folder, current)
    return current, range_changed


def settings_page(request):
    """Single per-folder configuration screen (PLAN T30, merging T18's
    accounts screen and T25's export settings): iCloud accounts, export
    destination/mode/date-prefix, and the working date range. The native
    folder picker for the export destination (window mode only) is
    client-side JS calling `window.pywebview.api.pick_folder()` -- see
    `window.py`'s `WindowApi`; this view never touches pywebview itself.

    PLAN T31: there is no more "Save settings" button -- each section
    auto-saves on `change` via htmx, posting only its own fields, handled by
    `_apply_partial_settings` above. An htmx POST gets back just the tiny
    "Saved" indicator partial for that section; a plain (non-htmx) POST
    still redirects back to a full page render (defensive fallback, e.g. JS
    disabled -- though every current form is htmx-driven).
    """
    folder = settings.WORKING_FOLDER

    if request.method == "POST":
        _, range_changed = _apply_partial_settings(folder, request.POST)
        if range_changed:
            _kick_pulls_for_accounts_with_session(folder)
        if request.headers.get("HX-Request") == "true":
            return render(request, "_saved_indicator.html")
        return redirect("settings")

    context = _settings_context(request)
    context.update(_accounts_context(request, next_page="settings"))
    return render(request, "settings.html", context)


def export_now(request):
    """ "Export now" (PLAN T25 UI): reachable from the grid filter bar and
    the settings page, both plain (non-htmx) POSTs -- always redirects back
    to the settings page, either with a "set a destination first" notice or
    to show the background-run progress banner there.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    current = folder_settings.load_settings(folder)
    if not current.export_destination:
        notice = "Set an export destination below before exporting."
        return redirect(f"{reverse('settings')}?notice={quote(notice)}")

    export.start_background_export(
        folder, Path(current.export_destination), date_prefix=current.export_date_prefix
    )
    return redirect("settings")


def export_status(request):
    """One step of the export banner's recursive load-polling -- mirrors
    `scan_status`/`_scan_banner.html`.
    """
    return render(request, "_export_progress.html", {"progress": _current_export_progress()})


# --- Setup wizard: working date range (PLAN T29) ----------------------------

# Sentinel "everything" range (distinct from "unset" -- see folder_settings.py
# docstring): open-started far enough back to include any real photo.
_EVERYTHING_FROM = "1970-01-01"


def _months_ago(today: date, months: int) -> date:
    """`today` minus `months` calendar months, clamping the day-of-month to
    the target month's last valid day (e.g. Mar 31 minus 1 month -> Feb 28/29,
    not an invalid Feb 31). No `dateutil` dependency (not in pyproject).
    """
    month_index = today.month - 1 - months
    year = today.year + month_index // 12
    month = month_index % 12 + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def setup(request):
    """Setup wizard (PLAN T29), gated on `grid` via `folder_settings.
    working_range` being unset. Step 1 (attach an iCloud account) only shows
    when no account is attached yet -- an attached account skips straight to
    step 2 (mirrors the brief: "step 2 (dates) ... when accounts exist, go
    straight to step 2"). `?step=2` (the "skip" link, and the grid's "edit
    range" link when accounts already exist) forces step 2 regardless.
    """
    folder = settings.WORKING_FOLDER
    has_accounts = bool(remote_state.list_accounts(folder))
    show_step1 = not has_accounts and request.GET.get("step") != "2"
    current = folder_settings.load_settings(folder)

    context = {
        "show_step1": show_step1,
        "has_accounts": has_accounts,
        "working_from": current.working_from,
        "working_to": current.working_to,
    }
    if show_step1:
        # Step 1 embeds the shared accounts section inline (PLAN T30) so
        # connect/2FA happen right on the wizard page, no detour to
        # Settings -- only needed while step 1 is actually shown.
        context.update(_accounts_context(request, next_page="setup"))
    return render(request, "setup.html", context)


def setup_dates(request):
    """Saves the working date range (preset or custom) and redirects to the
    grid, which is then unblocked by the gate above -- unless the POST
    carries `next=settings` (PLAN T30: the Settings page's own working-range
    section reuses this same endpoint), in which case it redirects back to
    Settings instead. Also kicks a background pull for every attached
    account with a live session -- SPEC intent: changing the range should
    immediately go fetch the newly-included backlog, not wait for the next
    manual "Pull now". Expired/never-attached sessions are skipped silently
    (same as an idle accounts screen -- this is a background nicety, not a
    hard requirement of saving the range).
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    preset = request.POST.get("preset", "")
    today = timezone.now().date()

    if preset == "everything":
        working_from, working_to = _EVERYTHING_FROM, ""
    elif preset == "last_month":
        working_from, working_to = _months_ago(today, 1).isoformat(), ""
    elif preset == "last_3_months":
        working_from, working_to = _months_ago(today, 3).isoformat(), ""
    elif preset == "last_year":
        working_from, working_to = _months_ago(today, 12).isoformat(), ""
    else:
        working_from = request.POST.get("from", "").strip()
        working_to = request.POST.get("to", "").strip()

    current = folder_settings.load_settings(folder)
    current.working_from = working_from
    current.working_to = working_to
    folder_settings.save_settings(folder, current)

    _kick_pulls_for_accounts_with_session(folder)

    if request.POST.get("next") == "settings":
        return redirect("settings")
    return redirect("grid")
