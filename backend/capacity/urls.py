"""Capacity forecast page. Mounted at /ops/capacity/ (config/urls.py)."""

from django.urls import path

from capacity import views

app_name = "capacity"

urlpatterns = [
    path("", views.capacity_page, name="report"),
]
