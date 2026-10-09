from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET


@require_GET
@ensure_csrf_cookie
def dashboard(request):
    """Serve the dashboard shell; all data comes from the JSON APIs."""
    return render(request, "production/dashboard.html")
