from django.urls import path

from hub import api, charging_views, stock_views, views

urlpatterns = [
    path("hub/", views.hub_page, name="hub"),
    path("hub/tasks/new/", views.task_new, name="hub-task-new"),
    path("hub/tasks/<int:task_id>/", views.task_detail, name="hub-task"),
    path("hub/tasks/<int:task_id>/<str:action>/", views.task_action, name="hub-task-action"),
    path("hub/charging/", charging_views.charging_page, name="hub-charging"),
    path("hub/charging/start/", charging_views.start_view, name="hub-charging-start"),
    path("hub/charging/sessions/<int:session_id>/end/", charging_views.end_view, name="hub-charging-end"),
    path("hub/charging/reserve/", charging_views.reserve_view, name="hub-charging-reserve"),
    path("hub/charging/soc/", charging_views.soc_view, name="hub-charging-soc"),
    path(
        "hub/charging/reservations/<int:reservation_id>/release/",
        charging_views.release_view,
        name="hub-charging-release",
    ),
    path("hub/charging/chargers/", charging_views.chargers_page, name="hub-chargers"),
    path("hub/charging/chargers/<str:code>/", charging_views.charger_service, name="hub-charger"),
    path("hub/stock/", stock_views.stock_page, name="hub-stock"),
    path("hub/stock/items/", stock_views.items_page, name="hub-stock-items"),
    path("hub/stock/receive/", stock_views.receive_view, name="hub-stock-receive"),
    path("hub/stock/issue/", stock_views.issue_view, name="hub-stock-issue"),
    path("hub/stock/return/", stock_views.return_view, name="hub-stock-return"),
    path("hub/stock/consume/", stock_views.consume_view, name="hub-stock-consume"),
    path("hub/stock/adjust/", stock_views.adjust_view, name="hub-stock-adjust"),
    path("hub/stock/reserve/", stock_views.reserve_view, name="hub-stock-reserve"),
    path(
        "hub/stock/reservations/<int:reservation_id>/release/",
        stock_views.release_view,
        name="hub-stock-release",
    ),
    path("api/hub/tasks", api.tasks, name="api-hub-tasks"),
    path("api/hub/tasks/<int:task_id>/<str:action>", api.task_action, name="api-hub-task-action"),
]
