from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render

from . import previews
from .models import Photo


def healthz(request):
    return HttpResponse("ok")


def home(request):
    return render(request, "base.html", {"message": "Culler — no folder UI yet"})


def preview(request, pk):
    try:
        photo = Photo.objects.get(pk=pk)
    except Photo.DoesNotExist as exc:
        raise Http404 from exc
    path = previews.preview_path(settings.WORKING_FOLDER, photo)
    response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
