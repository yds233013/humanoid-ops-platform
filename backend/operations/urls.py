"""Operations command center pages. Mounted at /ops/ (config/urls.py); the JSON API is operations.api."""

from django.urls import path

from operations import views

app_name = "operations"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("exceptions/", views.exceptions_page, name="exceptions"),
    path("incidents/", views.incidents_page, name="incidents"),
    path("timeline/", views.timeline_page, name="timeline"),
    path("metrics/", views.metrics_page, name="metrics"),
    path("closeout/", views.closeout_page, name="closeout"),
]
