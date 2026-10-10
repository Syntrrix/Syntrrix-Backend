from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from agro_app.views import BotWidgetHTMLAPIView

def home(request):
    return JsonResponse({
        "message": "Welcome to Agro API",
        "status": "running",
        "endpoints": {
            "admin": "/admin/",
            "api": "/api/"
        }
    })

urlpatterns = [
    path("", home, name="home"),  # Root URL
    path("admin/", admin.site.urls),
    path("api/", include("agro_app.urls")),
    path("widget/bot", BotWidgetHTMLAPIView.as_view(), name="widget-bot"),
]

from django.views.static import serve
from django.urls import re_path

# Serve media files (works in both DEBUG=True and DEBUG=False environments)
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
