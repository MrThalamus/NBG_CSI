from django.urls import path

from production import views
from production.modules.ack.views import ack_view
from production.modules.events.views import events_view
from production.modules.health.views import status_view
from production.modules.mqtt.views import mqtt_status_view
from production.modules.state.views import state_view

app_name = "production"

# Exact paths without trailing slashes: avoids APPEND_SLASH redirects on POST.
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/events", events_view, name="events"),
    path("api/state", state_view, name="state"),
    path("api/ack", ack_view, name="ack"),
    path("api/mqtt/status", mqtt_status_view, name="mqtt-status"),
    path("api/status", status_view, name="status"),
]
