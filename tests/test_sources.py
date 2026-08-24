"""Source registry + per-source sidecar decisions (SPEC §19, T28 -- M6 first
wave). Registry needs the Django DB (Source is a model); sidecar round-trip
is pure filesystem/JSON, mirroring test_remote_state.py's coverage.
"""

import json
from datetime import UTC, datetime

import pytest

from maier.core.models import Photo, Source, absolute_path_for, sentinel_for_source
from maier.core.sources import (
    SourceState,
    add_local_source,
    get_or_create_icloud_source,
    list_sources,
    load_source_state,
    record_decision,
    save_source_state,
)

# --- registry ----------------------------------------------------------


@pytest.mark.django_db
def test_add_local_source_registers_with_default_name(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    other = tmp_path / "apple-luis"
    other.mkdir()

    source = add_local_source(library, other)

    assert source.kind == Source.KIND_LOCAL
    assert source.name == "apple-luis"
    assert source.path == str(other.resolve())
    assert source.pk is not None


@pytest.mark.django_db
def test_add_local_source_explicit_name(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    other = tmp_path / "raw-export"
    other.mkdir()

    source = add_local_source(library, other, name="Maria's phone")

    assert source.name == "Maria's phone"


@pytest.mark.django_db
def test_add_local_source_dedupes_name(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    first = tmp_path / "camera"
    first.mkdir()
    second = tmp_path / "camera2"
    second.mkdir()
    third = tmp_path / "camera3"
    third.mkdir()

    s1 = add_local_source(library, first, name="camera")
    s2 = add_local_source(library, second, name="camera")
    s3 = add_local_source(library, third, name="camera")

    assert s1.name == "camera"
    assert s2.name == "camera (1)"
    assert s3.name == "camera (2)"


@pytest.mark.django_db
def test_add_local_source_rejects_path_inside_library(tmp_path):
    library = tmp_path / "library"
    (library / "sub").mkdir(parents=True)

    with pytest.raises(ValueError):
        add_local_source(library, library / "sub")

    with pytest.raises(ValueError):
        add_local_source(library, library)


@pytest.mark.django_db
def test_add_local_source_rejects_path_inside_another_source(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    root = tmp_path / "external"
    (root / "nested").mkdir(parents=True)

    add_local_source(library, root)

    with pytest.raises(ValueError):
        add_local_source(library, root / "nested")

    with pytest.raises(ValueError):
        add_local_source(library, root)  # re-adding the exact same path


@pytest.mark.django_db
def test_add_local_source_rejects_non_directory(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("hi")

    with pytest.raises(ValueError):
        add_local_source(library, a_file)


@pytest.mark.django_db
def test_add_local_source_rejects_missing_directory(tmp_path):
    library = tmp_path / "library"
    library.mkdir()

    with pytest.raises(ValueError):
        add_local_source(library, tmp_path / "does-not-exist")


@pytest.mark.django_db
def test_list_sources_orders_by_added(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    d1 = tmp_path / "d1"
    d1.mkdir()
    d2 = tmp_path / "d2"
    d2.mkdir()

    s1 = add_local_source(library, d1)
    s2 = add_local_source(library, d2)

    assert list(list_sources()) == [s1, s2]


@pytest.mark.django_db
def test_get_or_create_icloud_source(tmp_path):
    s1 = get_or_create_icloud_source("luis@example.com")
    s2 = get_or_create_icloud_source("luis@example.com")

    assert s1.pk == s2.pk
    assert s1.kind == Source.KIND_ICLOUD
    assert s1.name == "luis@example.com"
    assert s1.account == "luis@example.com"
    assert s1.path == ""


# --- sidecar state round-trip -------------------------------------------


def _make_source(tmp_path, name="apple-luis") -> Source:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return Source(kind=Source.KIND_LOCAL, name=name, path=str(path))


def test_load_source_state_missing_file_returns_fresh_empty_state(tmp_path):
    source = _make_source(tmp_path)

    state = load_source_state(source)

    assert state.decisions == {}
    assert state.version == 1


def test_save_then_load_round_trips(tmp_path):
    source = _make_source(tmp_path)
    state = SourceState(decisions={"IMG_0001.jpg": "selected", "sub/IMG_0002.jpg": "rejected"})

    save_source_state(source, state)
    loaded = load_source_state(source)

    assert loaded.decisions == {"IMG_0001.jpg": "selected", "sub/IMG_0002.jpg": "rejected"}


def test_save_source_state_writes_expected_json_schema(tmp_path):
    source = _make_source(tmp_path)
    state = SourceState(decisions={"a.jpg": "rejected"})

    save_source_state(source, state)

    path = tmp_path / "apple-luis" / "maier-state.json"
    data = json.loads(path.read_text())
    assert data["decisions"] == {"a.jpg": "rejected"}
    assert data["version"] == 1


def test_save_source_state_is_atomic_no_stray_tmp_files(tmp_path):
    source = _make_source(tmp_path)
    save_source_state(source, SourceState())

    names = {p.name for p in (tmp_path / "apple-luis").iterdir()}
    assert names == {"maier-state.json"}


def test_corrupt_source_state_quarantined_and_fresh_state_returned(tmp_path):
    source = _make_source(tmp_path)
    bad_path = tmp_path / "apple-luis" / "maier-state.json"
    bad_path.write_text("not valid json {{{")

    state = load_source_state(source)

    assert state.decisions == {}
    assert not bad_path.exists()
    quarantined = list((tmp_path / "apple-luis").glob("maier-state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "not valid json {{{"


def test_corrupt_source_state_not_silently_overwritten_by_next_save(tmp_path):
    source = _make_source(tmp_path)
    bad_path = tmp_path / "apple-luis" / "maier-state.json"
    bad_path.write_text("[1, 2, 3]")

    state = load_source_state(source)
    state.decisions["x"] = "rejected"
    save_source_state(source, state)

    quarantined = list((tmp_path / "apple-luis").glob("maier-state.json.corrupt-*"))
    assert len(quarantined) == 1
    reloaded = load_source_state(source)
    assert reloaded.decisions == {"x": "rejected"}


def test_record_decision_sets_and_removes(tmp_path):
    source = _make_source(tmp_path)

    record_decision(source, "IMG_0001.jpg", "rejected")
    assert load_source_state(source).decisions == {"IMG_0001.jpg": "rejected"}

    record_decision(source, "IMG_0001.jpg", "selected")
    assert load_source_state(source).decisions == {"IMG_0001.jpg": "selected"}

    record_decision(source, "IMG_0001.jpg", "optional")
    assert load_source_state(source).decisions == {}


# --- absolute_path_for ---------------------------------------------------


def _make_photo(**overrides) -> Photo:
    defaults = dict(
        relative_path="apple-luis/IMG_0001.jpg",
        status=Photo.STATUS_OPTIONAL,
        file_size=1,
        file_mtime=0.0,
        captured_at=datetime(2025, 1, 1, tzinfo=UTC),
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    defaults.update(overrides)
    return Photo(**defaults)


@pytest.mark.django_db
def test_absolute_path_for_library_root_row(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    photo = _make_photo(relative_path="apple-luis/IMG_0001.jpg")

    assert absolute_path_for(photo, library) == library / "apple-luis/IMG_0001.jpg"


@pytest.mark.django_db
def test_absolute_path_for_source_row(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    source_dir = tmp_path / "external"
    source_dir.mkdir()
    source = add_local_source(library, source_dir)

    photo = _make_photo(
        relative_path=sentinel_for_source(source, "sub/IMG_0001.jpg"),
        source_ref=source,
    )

    assert absolute_path_for(photo, library) == source_dir.resolve() / "sub/IMG_0001.jpg"


@pytest.mark.django_db
def test_absolute_path_for_icloud_row_raises(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    photo = _make_photo(
        source=Photo.SOURCE_ICLOUD,
        account="luis@example.com",
        remote_id="r1",
        relative_path="@icloud/luis@example.com/r1",
    )

    with pytest.raises(ValueError):
        absolute_path_for(photo, library)


@pytest.mark.django_db
def test_absolute_path_for_orphaned_source_sentinel_raises(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    photo = _make_photo(relative_path="@src/999/IMG_0001.jpg", source_ref=None)

    with pytest.raises(ValueError):
        absolute_path_for(photo, library)
