"""Thin request handlers -- filtering/grouping logic lives in queries.py,
file moves in moves.py, previews in previews.py. See SPEC §10 for the UI
spec these implement.
"""

from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

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
    updates,
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

    medium_dest = previews.remote_medium_dest(folder, photo.account, photo.remote_id or "")
    path = previews.best_remote_preview(folder, photo)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    if path == medium_dest:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "no-store"
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
        photo.download_pending = _download_pending(photo)

    scan_progress = _in_flight_scan_progress()

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
        "filter_dates_low": filters.get("dates") == "low",
        "unresolved_pair_count": phaseb.unresolved_pair_count(),
        "missing_count": queries.missing_photo_count(),
        "show_missing": filters.get("show") == "missing",
        "total_photo_count": queries.total_photo_count(),
        "scanning": scan_progress is not None,
        "scan_progress": scan_progress,
        "update_info": updates.latest_known_update(),
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
    for fphoto in filmstrip:
        fphoto.download_pending = _download_pending(fphoto)

    dupe_count = phaseb.duplicate_counts().get(photo.sha256, 0) if photo.sha256 else 0
    photo.download_pending = _download_pending(photo)

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


def _accounts_context(request, **extra) -> dict:
    context = {
        "accounts": _accounts_rows(settings.WORKING_FOLDER),
        "download_errors": _recent_download_errors(),
        "added": request.GET.get("added", ""),
        # T21 two-step confirm: `?confirm=<email>` re-renders the accounts
        # page with an inline confirmation block for that one row instead of
        # a JS confirm() dialog (CLAUDE.md hard rule 4).
        "confirm_disconnect": request.GET.get("confirm", ""),
        "disconnected": request.GET.get("disconnected", ""),
    }
    context.update(extra)
    return context


def accounts(request):
    return render(request, "accounts.html", _accounts_context(request))


def account_disconnect(request):
    """ "Disconnect" (SPEC §18 UI, PLAN T21): removes the account's saved
    session + its remote DB rows + cached previews. Keeps
    `icloud-state/{slug}.json` (durable decisions -- re-attaching restores
    rejections) and everything already in `selected/` (ordinary local files
    by then). Two-step confirm lives entirely in `accounts.html` (GET
    `?confirm=<email>` renders the inline confirm block below); this view is
    only the POST that actually performs it.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("account", "").strip()

    try:
        disconnect.disconnect_account(folder, email)
    except disconnect.PullInFlight as exc:
        context = _accounts_context(request, disconnect_error=str(exc))
        return render(request, "accounts.html", context)

    return redirect(f"{reverse('accounts')}?disconnected={email}")


def account_login(request):
    """Add-account form target (SPEC §18 UI). The password lives only in
    `request.POST` for the duration of this request -- it is passed
    straight to `ICloudClient.login` and never assigned to any attribute,
    logged, or written to disk (CLAUDE.md hard rule 8).
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "")

    try:
        logged_in = ICloudClient.login(email, password)
    except TwoFactorRequired as exc:
        # Single-user localhost app (see `_pending_2fa` module docstring):
        # stash the already-authenticated-pending client so the 2FA POST
        # below submits the code on the SAME pyicloud session.
        _pending_2fa[email] = exc.client
        context = _accounts_context(request, pending_2fa_email=email)
        return render(request, "accounts.html", context)
    except ICloudError as exc:
        context = _accounts_context(request, login_error=str(exc), prefill_email=email)
        return render(request, "accounts.html", context)

    # No 2FA required: ensure a state file exists so the account shows up
    # in list_accounts() even before its first pull.
    remote_state.save_state(folder, remote_state.load_state(folder, email))
    # Auto-start the first pull: a freshly attached account with no photos
    # is a UX trap (authenticated-but-empty timeline, 2026-08-24 incident).
    pull.start_background_pull(folder, logged_in)
    return redirect(f"{reverse('accounts')}?added={email}")


def account_2fa(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("email", "").strip()
    code = request.POST.get("code", "")

    client = _pending_2fa.pop(email, None)
    if client is None:
        context = _accounts_context(
            request,
            login_error=f"No pending login for {email} -- please log in again below.",
        )
        return render(request, "accounts.html", context)

    try:
        verified = client.submit_2fa(code)
    except ICloudError as exc:
        _pending_2fa[email] = client  # keep the pending session for a retry
        context = _accounts_context(request, pending_2fa_email=email, twofa_error=str(exc))
        return render(request, "accounts.html", context)

    if not verified:
        _pending_2fa[email] = client  # keep the pending session for a retry
        context = _accounts_context(
            request, pending_2fa_email=email, twofa_error="Incorrect verification code."
        )
        return render(request, "accounts.html", context)

    remote_state.save_state(folder, remote_state.load_state(folder, email))
    # Auto-start the first pull on the just-verified client (same UX-trap
    # fix as account_login's success path).
    pull.start_background_pull(folder, client)
    return redirect(f"{reverse('accounts')}?added={email}")


def account_pull(request):
    """ "Pull now" (SPEC §18 UI). `account` is a POST field rather than a
    path segment (flagged, PLAN T18 brief: avoids mapping an arbitrary
    Apple-ID email on/off a URL-safe slug for routing purposes -- the slug
    is still used for filenames/dirs elsewhere via `remote_state.account_slug`).
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    folder = settings.WORKING_FOLDER
    email = request.POST.get("account", "").strip()

    client = ICloudClient.from_session(email)
    if client is None:
        context = _accounts_context(
            request,
            pull_error=f"iCloud session for {email} has expired -- please re-authenticate below.",
            prefill_email=email,
        )
        return render(request, "accounts.html", context)

    pull.start_background_pull(folder, client)
    # Resumes any previously-stuck selected-pending downloads too (e.g. rows
    # left behind by a session expiry on an earlier worker run).
    downloads.start_worker(folder)
    return redirect("accounts")


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


def settings_page(request):
    """Per-folder settings screen (PLAN T25): export destination/mode/date-
    prefix, backed by `folder_settings.json`. A native folder picker isn't
    wired here (flagged, per brief): pywebview's file dialog must run on the
    main thread, which a Django request thread never is -- the text input is
    the only picker in both browser and window mode for now; a proper
    desktop-window "Choose..." button is a follow-up.
    """
    folder = settings.WORKING_FOLDER

    if request.method == "POST":
        dest = request.POST.get("export_destination", "").strip()
        mode = request.POST.get("export_mode", folder_settings.MODE_MANUAL)
        if mode not in (folder_settings.MODE_MANUAL, folder_settings.MODE_AUTOMATIC):
            mode = folder_settings.MODE_MANUAL
        date_prefix = request.POST.get("export_date_prefix") == "on"
        folder_settings.save_settings(
            folder,
            folder_settings.FolderSettings(
                export_destination=dest, export_mode=mode, export_date_prefix=date_prefix
            ),
        )
        return redirect("settings")

    context = {
        "export_settings": folder_settings.load_settings(folder),
        "notice": request.GET.get("notice", ""),
        "export_progress": _current_export_progress(),
    }
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
