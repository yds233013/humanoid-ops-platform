"""Small non-API pages shared by the whole app."""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

from platform_core.templatetags.ui import landing_url, request_roles


@login_required
def landing(request: HttpRequest) -> HttpResponse:
    """After login: Today (/ops/) for roles that may read it, else the fleet list."""
    return redirect(landing_url(request_roles(request)))
