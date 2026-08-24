"""`maier.window`'s js_api (PLAN T30): `WindowApi.pick_folder` is the only
piece of pywebview interaction that's meaningfully unit-testable without a
real native window -- it's a small wrapper around
`self._window.create_file_dialog(...)`, so a fake `_window` stand-in
exercises its return-value contract (first path / None) without touching
any actual GUI backend. `launch_window`/`pick_folder`/`show_home` (the
window-creating functions) stay untested here, same as before this task --
they require a real display and are only exercised manually (PLAN T11
decisions log).

PLAN T31: `_folder_dialog_kind` also gets direct coverage against a fake
`webview` module stand-in, exercising both the modern
`webview.FileDialog.FOLDER` branch and the deprecated `webview.FOLDER_DIALOG`
fallback (older pywebview with no `FileDialog` attribute at all) without
importing the real `webview` package.
"""

from types import SimpleNamespace

from maier.window import WindowApi, _folder_dialog_kind


class _FakeWindow:
    def __init__(self, paths):
        self._paths = paths
        self.dialog_calls = 0

    def create_file_dialog(self, dialog_type):
        self.dialog_calls += 1
        return self._paths


def test_pick_folder_returns_first_selected_path():
    api = WindowApi()
    api._window = _FakeWindow(["/Users/someone/Pictures", "/ignored"])

    result = api.pick_folder()

    assert result == "/Users/someone/Pictures"


def test_pick_folder_returns_none_when_dialog_cancelled():
    api = WindowApi()
    api._window = _FakeWindow(None)

    assert api.pick_folder() is None


def test_pick_folder_returns_none_when_dialog_returns_empty_list():
    api = WindowApi()
    api._window = _FakeWindow([])

    assert api.pick_folder() is None


def test_pick_folder_returns_none_before_window_is_set():
    api = WindowApi()

    assert api.pick_folder() is None


def test_pick_folder_returns_none_on_dialog_exception():
    class _RaisingWindow:
        def create_file_dialog(self, dialog_type):
            raise RuntimeError("no display")

    api = WindowApi()
    api._window = _RaisingWindow()

    assert api.pick_folder() is None


def test_pick_folder_uses_modern_file_dialog_kind():
    api = WindowApi()
    window = _FakeWindow(["/Users/someone/Pictures"])
    api._window = window

    result = api.pick_folder()

    assert result == "/Users/someone/Pictures"
    assert window.dialog_calls == 1


def test_folder_dialog_kind_prefers_modern_file_dialog_enum():
    fake_webview = SimpleNamespace(
        FileDialog=SimpleNamespace(FOLDER="modern-folder-kind"),
        FOLDER_DIALOG="deprecated-folder-kind",
    )

    assert _folder_dialog_kind(fake_webview) == "modern-folder-kind"


def test_folder_dialog_kind_falls_back_to_deprecated_constant():
    fake_webview = SimpleNamespace(FOLDER_DIALOG="deprecated-folder-kind")

    assert _folder_dialog_kind(fake_webview) == "deprecated-folder-kind"
