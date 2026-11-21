from django.urls import path

from planning import views

urlpatterns = [
    path("", views.week, name="planning-week"),
    path("book/", views.book_visit, name="planning-book"),
    # Customers and homes (order matters: "new" before the <code> pages)
    path("sites/", views.sites, name="planning-sites"),
    path("sites/new/", views.site_new, name="planning-site-new"),
    path("sites/<str:code>/", views.site_detail, name="planning-site"),
    path("sites/<str:code>/adults/", views.site_adults, name="planning-site-adults"),
    path("sites/<str:code>/assessments/new/", views.assessment_new, name="planning-assessment-new"),
    path(
        "sites/<str:code>/assessments/<int:assessment_id>/approve/",
        views.assessment_approve,
        name="planning-assessment-approve",
    ),
    path("sites/<str:code>/consents/new/", views.consent_new, name="planning-consent-new"),
    path(
        "sites/<str:code>/consents/<int:consent_id>/withdraw/",
        views.consent_withdraw,
        name="planning-consent-withdraw",
    ),
    path("sites/<str:code>/book/", views.book_visit, name="planning-site-book"),
    # Visits
    path("visits/<str:code>/", views.visit_detail, name="planning-visit"),
    path("visits/<str:code>/plan/", views.plan_view, name="planning-visit-plan"),
    path("visits/<str:code>/confirm/", views.confirm_view, name="planning-visit-confirm"),
    path("visits/<str:code>/cancel/", views.cancel_view, name="planning-visit-cancel"),
]
