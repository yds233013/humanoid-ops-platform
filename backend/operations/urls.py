"""Operations command center pages. Mounted at /ops/ (config/urls.py); the JSON API is operations.api."""

from django.urls import path

from operations import views

app_name = "operations"

urlpatterns = [
    path("", views.today_page, name="today"),
    path("robots/", views.robot_status_page, name="robot-status"),
    path("production-readiness/", views.production_readiness_page, name="production-readiness"),
    path("exceptions/", views.exceptions_page, name="exceptions"),
    path("incidents/", views.incidents_page, name="incidents"),
    path("timeline/", views.timeline_page, name="timeline"),
    path("metrics/", views.metrics_page, name="metrics"),
    path("health/", views.health_page, name="health"),
    path("closeout/", views.closeout_page, name="closeout"),
]
