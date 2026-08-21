from django.urls import path

from culler.core import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("grid", views.grid, name="grid"),
    path("review/<int:pk>", views.review, name="review"),
    path("set-status/<int:pk>", views.set_status, name="set-status"),
    path("scan-status", views.scan_status, name="scan-status"),
    path("rescan", views.rescan, name="rescan"),
    path("preview/<int:pk>", views.preview, name="preview"),
]
