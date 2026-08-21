from django.db import models


class Photo(models.Model):
    STATUS_OPTIONAL = "optional"
    STATUS_SELECTED = "selected"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_OPTIONAL, "Optional"),
        (STATUS_SELECTED, "Selected"),
        (STATUS_REJECTED, "Rejected"),
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

    class Meta:
        ordering = ["captured_at"]

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
