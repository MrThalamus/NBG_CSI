from django.http import JsonResponse

from production.modules.mqtt.service import get_mqtt_status
from production.shared.http import api_endpoint


@api_endpoint(["GET"])
def mqtt_status_view(request):
    return JsonResponse(get_mqtt_status())
