from django.http import HttpResponse
from django.shortcuts import render


def healthz(request):
    return HttpResponse("ok")


def home(request):
    return render(request, "base.html", {"message": "Culler — no folder UI yet"})
