"""pywebview desktop shell: main window launcher, native folder picker, and
the pre-Django home screen (recent folders / "Open folder...").

All `pywebview` interaction is isolated to this module and imported lazily
(inside functions), so:
  - the module stays importable in headless CI/dev environments where
    pywebview's native backends (WebKit/GTK/WebView2) may be missing, and
  - tests never trigger a real native window.

`pick_folder` / `show_home` are best-effort: pywebview windowing can't be
exercised in an automated test environment, so both are wrapped defensively
and return `None` on any failure rather than raising.
"""

from __future__ import annotations

import html
from pathlib import Path


def _folder_dialog_kind(webview_module):
    """Compat shim (PLAN T31): recent pywebview versions deprecated the
    top-level `webview.FOLDER_DIALOG` constant in favour of
    `webview.FileDialog.FOLDER`, and log a deprecation warning on every use
    of the old name. Prefer the new enum when present, fall back to the old
    constant on older pywebview releases that don't have `FileDialog` yet.
    Takes the already-imported `webview` module rather than importing it
    itself, matching this module's lazy-import-inside-functions pattern
    (module docstring above) so this helper stays importable headless too.
    """
    file_dialog = getattr(webview_module, "FileDialog", None)
    if file_dialog is not None:
        return file_dialog.FOLDER
    return webview_module.FOLDER_DIALOG


class WindowApi:
    """js_api exposed to the running app window as `window.pywebview.api`
    (PLAN T30): lets the settings page's "Choose folder..." button open a
    native folder picker. js_api methods are dispatched by pywebview onto
    its own GUI thread internally -- this is the documented, safe pattern,
    unlike calling `create_file_dialog` directly from a Django request
    thread (pywebview's dialog must run on the thread that owns the
    window). `_window` is set by `launch_window` right after the window is
    created, before `webview.start()` blocks.
    """

    def __init__(self) -> None:
        self._window = None

    def pick_folder(self) -> str | None:
        if self._window is None:
            return None
        import webview

        try:
            paths = self._window.create_file_dialog(_folder_dialog_kind(webview))
        except Exception:
            paths = None
        if not paths:
            return None
        return paths[0]


def launch_window(url: str, title: str = "Maier") -> None:
    """Open the main app window pointed at `url` and block until closed.
    Must be called from the main thread (pywebview owns it)."""
    import webview

    api = WindowApi()
    window = webview.create_window(title, url, width=1280, height=860, js_api=api)
    api._window = window
    webview.start()


def pick_folder() -> Path | None:
    """Pre-boot native folder picker (no app window/server running yet).

    pywebview requires the file dialog to run after `webview.start()`; the
    established pattern is `webview.start(func, window)`, where `func` runs
    on the GUI thread once it's ready, opens the dialog, and destroys the
    window so `start()` returns.
    """
    try:
        import webview
    except Exception:
        return None

    result: list[str] = []

    def _run(window) -> None:
        try:
            paths = window.create_file_dialog(_folder_dialog_kind(webview))
        except Exception:
            paths = None
        if paths:
            result.append(paths[0])
        window.destroy()

    try:
        window = webview.create_window("Choose a folder", hidden=True)
        webview.start(_run, window)
    except Exception:
        return None

    if not result:
        return None
    return Path(result[0])


def _home_html(recents: list[dict]) -> str:
    if recents:
        items = []
        for entry in recents:
            path = str(entry.get("path", ""))
            last_opened = str(entry.get("last_opened", ""))
            name = Path(path).name or path
            items.append(
                f'<li class="recent-item" data-path="{html.escape(path)}">'
                f'<div class="recent-name">{html.escape(name)}</div>'
                f'<div class="recent-path">{html.escape(path)}</div>'
                f'<div class="recent-meta">last opened {html.escape(last_opened)}</div>'
                "</li>"
            )
        list_html = f'<ul class="recent-list">{"".join(items)}</ul>'
    else:
        list_html = '<p class="empty">No recent folders yet.</p>'

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    margin: 0;
    background: #14161a;
    color: #e8e8ea;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 48px 24px;
  }}
  h1 {{ font-weight: 600; font-size: 1.4rem; margin: 0 0 24px; }}
  .recent-list {{ list-style: none; padding: 0; margin: 0 0 32px; width: 100%; max-width: 520px; }}
  .recent-item {{
    padding: 12px 16px;
    margin-bottom: 8px;
    border-radius: 8px;
    background: #1d2026;
    cursor: pointer;
    transition: background 0.1s ease;
  }}
  .recent-item:hover {{ background: #262a33; }}
  .recent-name {{ font-weight: 600; }}
  .recent-path, .recent-meta {{ font-size: 0.8rem; color: #9a9aa2; }}
  .empty {{ color: #9a9aa2; margin-bottom: 32px; }}
  button {{
    background: #4f8cff;
    color: #14161a;
    border: none;
    border-radius: 8px;
    padding: 12px 24px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
  }}
  button:hover {{ opacity: 0.9; }}
</style>
</head>
<body>
  <h1>Maier</h1>
  {list_html}
  <button id="pick-btn">Open folder&hellip;</button>
  <script>
    document.querySelectorAll('.recent-item').forEach(function (el) {{
      el.addEventListener('click', function () {{
        window.pywebview.api.open_recent(el.dataset.path);
      }});
    }});
    document.getElementById('pick-btn').addEventListener('click', function () {{
      window.pywebview.api.pick_and_open();
    }});
  </script>
</body>
</html>"""


def show_home(recents: list[dict]) -> Path | None:
    """Show the pre-Django home screen (recent folders + native picker).
    Returns the chosen folder, or None if the window was closed without
    a choice (or pywebview is unavailable / fails for any reason)."""
    try:
        import webview
    except Exception:
        return None

    class HomeApi:
        def __init__(self) -> None:
            self.chosen: Path | None = None

        def open_recent(self, path: str) -> None:
            self.chosen = Path(path)
            webview.windows[0].destroy()

        def pick_and_open(self) -> None:
            window = webview.windows[0]
            try:
                paths = window.create_file_dialog(_folder_dialog_kind(webview))
            except Exception:
                paths = None
            if paths:
                self.chosen = Path(paths[0])
            window.destroy()

    api = HomeApi()
    try:
        webview.create_window(
            "Maier",
            html=_home_html(recents),
            js_api=api,
            width=900,
            height=640,
        )
        webview.start()
    except Exception:
        return None

    return api.chosen
