from django.urls import path

from maier.core import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("grid", views.grid, name="grid"),
    path("review/<int:pk>", views.review, name="review"),
    path("set-status/<int:pk>", views.set_status, name="set-status"),
    path("scan-status", views.scan_status, name="scan-status"),
    path("rescan", views.rescan, name="rescan"),
    path("preview/<int:pk>", views.preview, name="preview"),
    path("stream/<int:pk>", views.stream, name="stream"),
    path("dupes", views.dupes, name="dupes"),
    path("dupes/<int:pair_id>/resolve", views.resolve_pair, name="resolve-pair"),
    path("summary", views.summary, name="summary"),
    path("accounts", views.accounts, name="accounts"),
    path("accounts/login", views.account_login, name="account-login"),
    path("accounts/2fa", views.account_2fa, name="account-2fa"),
    path("accounts/pull", views.account_pull, name="account-pull"),
    path("pull-status", views.pull_status, name="pull-status"),
]
