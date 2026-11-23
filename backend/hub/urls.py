from django.urls import path

from hub import api, views

urlpatterns = [
    path("hub/", views.hub_page, name="hub"),
    path("hub/tasks/new/", views.task_new, name="hub-task-new"),
    path("hub/tasks/<int:task_id>/", views.task_detail, name="hub-task"),
    path("hub/tasks/<int:task_id>/<str:action>/", views.task_action, name="hub-task-action"),
    path("api/hub/tasks", api.tasks, name="api-hub-tasks"),
    path("api/hub/tasks/<int:task_id>/<str:action>", api.task_action, name="api-hub-task-action"),
]
