from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse

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
]
