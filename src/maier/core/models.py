from pathlib import Path, PurePosixPath

from django.db import models
from django.db.models import Q


class Source(models.Model):
    """A registered library source (SPEC §19, M6 first wave). Not yet wired
    into views/culling -- this is the data model + indexing plumbing only;
    the app still boots single-folder (MAIER_FOLDER) for every existing code
    path. `Photo.source_ref` points here for rows indexed from a source.
    """

    KIND_LOCAL = "local"
    KIND_ICLOUD = "icloud"
    KIND_CHOICES = [
        (KIND_LOCAL, "Local"),
        (KIND_ICLOUD, "iCloud"),
    ]

    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    # display name / provenance tag; unique so it can double as a stable
    # human-readable label (folder basename, de-duped, or account email).
    name = models.CharField(max_length=255, unique=True)
    # absolute path for kind="local"; "" for kind="icloud" (no local root).
    path = models.CharField(max_length=4096, blank=True, default="")
    # Apple-ID email for kind="icloud"; "" for kind="local".
    account = models.CharField(max_length=255, blank=True, default="")
    added_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Photo(models.Model):
    STATUS_OPTIONAL = "optional"
    STATUS_SELECTED = "selected"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_OPTIONAL, "Optional"),
        (STATUS_SELECTED, "Selected"),
        (STATUS_REJECTED, "Rejected"),
    ]

    # SPEC §18 / CLAUDE.md hard rules 8-9: remote (iCloud) rows have no local
    # file until selected; they carry `account` + `remote_id` instead of a
    # real `relative_path`.
    SOURCE_LOCAL = "local"
    SOURCE_ICLOUD = "icloud"
    SOURCE_CHOICES = [
        (SOURCE_LOCAL, "Local"),
        (SOURCE_ICLOUD, "iCloud"),
    ]

    MEDIA_IMAGE = "image"
    MEDIA_VIDEO = "video"
    MEDIA_TYPE_CHOICES = [
        (MEDIA_IMAGE, "Image"),
        (MEDIA_VIDEO, "Video"),
    ]

    CAPTURED_AT_SOURCE_CHOICES = [
        ("exif", "EXIF"),
        ("filename", "Filename"),
        ("file_mtime", "File mtime"),
    ]

    # unique, POSIX-style, current location incl. status prefix
    relative_path = models.CharField(max_length=4096, unique=True, db_index=True)
    # derived from location on every scan/move; cached for queries
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_OPTIONAL, db_index=True
    )
    # T24: `selected/` is flat (no mirrored substructure), so its location
    # alone can't recover a photo's pre-select path for unflag/reject-from-
    # selected. Set once by `core/moves.apply_status` whenever a photo moves
    # FROM a non-status location (never overwritten after that -- stable
    # round trips per PLAN T24 rule 4). "" for photos that have never left a
    # non-status location, and for iCloud downloads (PLAN T24 rule 6 --
    # deliberately left empty, see `core/downloads.py`).
    original_path = models.CharField(max_length=4096, blank=True, default="")
    # first non-status path segment, "" for root files
    provenance = models.CharField(max_length=255, blank=True, db_index=True)
    file_size = models.BigIntegerField()
    file_mtime = models.FloatField()
    sha256 = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    phash = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    captured_at = models.DateTimeField(db_index=True)
    captured_at_source = models.CharField(max_length=16, choices=CAPTURED_AT_SOURCE_CHOICES)
    media_type = models.CharField(max_length=8, choices=MEDIA_TYPE_CHOICES)
    live_photo_video_path = models.CharField(max_length=4096, null=True, blank=True)
    missing = models.BooleanField(default=False)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    indexed_at = models.DateTimeField(auto_now=True)

    # source="local" (default) is an ordinary indexed file. source="icloud"
    # rows have `relative_path` set to the sentinel
    # f"@icloud/{account}/{remote_id}" (never a real path) until selection
    # downloads the original and converts the row to source="local" (T17).
    source = models.CharField(
        max_length=8, choices=SOURCE_CHOICES, default=SOURCE_LOCAL, db_index=True
    )
    # Apple-ID email for source="icloud" rows; "" for local rows.
    account = models.CharField(max_length=255, blank=True, default="")
    remote_id = models.CharField(max_length=255, null=True, blank=True)
    # Original filename reported by the API (e.g. "IMG_0001.HEIC"); used as
    # the download destination's filename (T17 core/downloads.py). "" until
    # a pull populates it; falls back to f"{remote_id}.jpg" if still empty.
    remote_filename = models.CharField(max_length=255, blank=True, default="")

    # T28 (M6 first wave): registered-source rows carry this FK; legacy rows
    # (indexed from the library root, the current single-folder world) are
    # source_ref=None -- deliberately nullable so every existing row/test
    # keeps working untouched. Rows with source_ref set use the sentinel
    # relative_path f"@src/{source_ref.pk}/{rel-within-source}" (mirrors the
    # existing "@icloud/..." sentinel pattern) since relative_path must stay
    # globally unique across the whole library, not just within one source.
    source_ref = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="photos"
    )

    class Meta:
        ordering = ["captured_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "remote_id"],
                condition=Q(remote_id__isnull=False),
                name="unique_account_remote_id_when_present",
            ),
        ]

    @property
    def provenance_display(self) -> str:
        """Explicit origin for the UI (CTO, 2026-08-25): "iCloud · account"
        for remote-born rows (including downloaded ones, which keep their
        `account`), "Local · folder" for filesystem rows.
        """
        if self.account:
            return f"iCloud \u00b7 {self.account}"
        return f"Local \u00b7 {self.provenance or 'root'}"

    def __str__(self) -> str:
        return self.relative_path


class DuplicatePair(models.Model):
    photo_a = models.ForeignKey(
        Photo, related_name="duplicate_pairs_as_a", on_delete=models.CASCADE
    )
    photo_b = models.ForeignKey(
        Photo, related_name="duplicate_pairs_as_b", on_delete=models.CASCADE
    )
    hamming_distance = models.PositiveSmallIntegerField()
    resolved = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"{self.photo_a_id} <-> {self.photo_b_id} (d={self.hamming_distance})"


# T28 (M6 first wave): sentinel prefix for source-indexed rows' relative_path
# (mirrors the existing "@icloud/..." sentinel used for remote rows).
SOURCE_SENTINEL_PREFIX = "@src/"


def sentinel_for_source(source: Source, rel: str) -> str:
    """`@src/{source.pk}/{rel}` -- the globally-unique relative_path stored
    for a row indexed from a registered source. Single implementation shared
    by `core/scan.py` (writes it) and `core/sources.py`/callers (parse it).
    """
    return f"{SOURCE_SENTINEL_PREFIX}{source.pk}/{rel}"


def rel_from_source_sentinel(sentinel: str) -> str:
    """Inverse of `sentinel_for_source`: the path within the source. Returns
    "" if `sentinel` isn't a well-formed `@src/...` sentinel.
    """
    if not sentinel.startswith(SOURCE_SENTINEL_PREFIX):
        return ""
    parts = sentinel.split("/", 2)
    return parts[2] if len(parts) > 2 else ""


def absolute_path_for(photo: Photo, library: Path) -> Path:
    """Resolve `photo`'s real filesystem location (T28 brief -- later tasks
    route previews/streaming/culling through this instead of hard-coding
    `library / photo.relative_path`).

    - Library-root rows (relative_path is a plain path, no sentinel):
      `library / photo.relative_path`, same as every existing code path.
    - `@src/{pk}/{rel}` rows: `Path(photo.source_ref.path) / rel`. Raises
      ValueError if `source_ref` is missing (e.g. the Source row was deleted
      -- `on_delete=SET_NULL` leaves an orphaned sentinel path behind).
    - `@icloud/...` rows: always raises ValueError -- remote rows have no
      local file until selection converts them to an ordinary local row
      (SPEC §18, CLAUDE.md hard rule 9).
    """
    rel = photo.relative_path
    if rel.startswith("@icloud/"):
        raise ValueError(f"iCloud row has no local file: {rel}")
    if rel.startswith(SOURCE_SENTINEL_PREFIX):
        if photo.source_ref_id is None:
            raise ValueError(f"source row missing source_ref: {rel}")
        rel_in_source = rel_from_source_sentinel(rel)
        return Path(photo.source_ref.path) / PurePosixPath(rel_in_source)
    return Path(library) / rel
