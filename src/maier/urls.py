from django.urls import path

from maier.core import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("setup", views.setup, name="setup"),
    path("setup/dates", views.setup_dates, name="setup-dates"),
    path("grid", views.grid, name="grid"),
    path("review/<int:pk>", views.review, name="review"),
    path("set-status/<int:pk>", views.set_status, name="set-status"),
    path("scan-status", views.scan_status, name="scan-status"),
    path("rescan", views.rescan, name="rescan"),
    path("preview/<int:pk>", views.preview, name="preview"),
    path("preview-sharp/<int:pk>", views.preview_sharp, name="preview-sharp"),
    path("sharp-status/<int:pk>", views.sharp_status, name="sharp-status"),
    path("stream/<int:pk>", views.stream, name="stream"),
    path("dupes", views.dupes, name="dupes"),
    path("dupes/<int:pair_id>/resolve", views.resolve_pair, name="resolve-pair"),
    path("summary", views.summary, name="summary"),
    path("accounts", views.accounts, name="accounts"),
    path("accounts/login", views.account_login, name="account-login"),
    path("accounts/2fa", views.account_2fa, name="account-2fa"),
    path("accounts/pull", views.account_pull, name="account-pull"),
    path("accounts/disconnect", views.account_disconnect, name="account-disconnect"),
    path("pull-status", views.pull_status, name="pull-status"),
    path("settings", views.settings_page, name="settings"),
    path("export-now", views.export_now, name="export-now"),
    path("export-status", views.export_status, name="export-status"),
]
