import os

from pathlib import Path
from datetime import timedelta
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-o-ut=c-(*&@+i$d3@wanni5+_&zvno%iahsq*_w3%4^9@p26=w"

# Set DEBUG = True for local development to enable media file serving
DEBUG = True

# QA API must include its own hostname or Django returns 400 before CORS runs (browser may show "CORS").
ALLOWED_HOSTS = [
    "3.110.156.7",
    "localhost",
    "*",
    "api.qa.syntrrix.com",
]
_extra_allowed_hosts = [h.strip() for h in os.environ.get("DJANGO_EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()]
if _extra_allowed_hosts:
    ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, *_extra_allowed_hosts]))


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "agro_app",
    "corsheaders",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "agro_main.urls"

# Bot builder PUTs send large JSON. Nginx must allow the body (default ~1m → 413).
# After nginx, Django still caps in-memory parse size unless raised:
_data_upload_mb = int(os.environ.get("DATA_UPLOAD_MAX_MEMORY_MB", "100"))
DATA_UPLOAD_MAX_MEMORY_SIZE = max(_data_upload_mb, 1) * 1024 * 1024

# CORS — production/QA:
# 1) Set CORS_ALLOWED_ORIGINS to EVERY exact SPA origin (scheme + host, no path). Example:
#    CORS_ALLOWED_ORIGINS=https://qa.syntrrix.com,https://app.syntrrix.com
# 2) OR set CORS_ALLOWED_ORIGIN_REGEX (one Python regex) to allow many subdomains, e.g.
#    CORS_ALLOWED_ORIGIN_REGEX=^https://([a-z0-9-]+\.)*syntrrix\.com$
# If BOTH are unset → CORS_ALLOW_ALL_ORIGINS=True (fine for local dev only).
#
# "Only /api/bots PUT shows CORS": often NOT Django CORS — nginx returns 413/502 with an HTML
# error page that has NO Access-Control-Allow-Origin → browser reports CORS. Fix nginx body
# limit + add CORS headers on error responses (see deploy/nginx-api-large-bodies.conf.example).
_cors_allowed = [x.strip() for x in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if x.strip()]
_cors_origin_regex = os.environ.get("CORS_ALLOWED_ORIGIN_REGEX", "").strip()

if _cors_allowed or _cors_origin_regex:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = _cors_allowed
    CORS_ALLOWED_ORIGIN_REGEXES = [_cors_origin_regex] if _cors_origin_regex else []
else:
    CORS_ALLOW_ALL_ORIGINS = True
    CORS_ALLOWED_ORIGINS = []
    CORS_ALLOWED_ORIGIN_REGEXES = []

CORS_ALLOW_CREDENTIALS = True
# JWT + JSON PUT preflight
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "agro_main.wsgi.application"


# DATABASES = {
#     'default': {    
#         'ENGINE': 'django.db.backends.mysql',
#         'NAME': 'agro_db',
#         'USER': 'root',
#         'PASSWORD': 'root',
#         'HOST': 'localhost',
#         'PORT': '3306',
#         'OPTIONS': {
#             'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
#         },
#     },
# }



# ------ New Db Connection 15-06-2026   ---------------------

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'database-1',
        'USER': 'admin',
        'PASSWORD': 'Database123',
        'HOST': 'database-1.ctokeeo06ck9.ap-south-1.rds.amazonaws.com',
        'PORT': '3306',
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            'charset': 'utf8mb4',
        },
    }
}




CSRF_TRUSTED_ORIGINS = ['http://3.110.156.7']
_extra_csrf = [x.strip() for x in os.environ.get("CSRF_TRUSTED_ORIGINS_EXTRA", "").split(",") if x.strip()]
if _extra_csrf:
    CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([*CSRF_TRUSTED_ORIGINS, *_extra_csrf]))
CSRF_COOKIE_SECURE = False 
CSRF_COOKIE_HTTPONLY = False 
CSRF_COOKIE_SAMESITE = 'Lax'

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "agro_app.authentication.AgroJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=7),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "SIGNING_KEY": SECRET_KEY,
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = "/var/www/django_static"

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
    
# Persistent non-database JSON storage path for Data Hub and Live Chat
PERSISTENT_DATA_ROOT = os.environ.get("PERSISTENT_DATA_ROOT")
if not PERSISTENT_DATA_ROOT:
    if os.name == 'nt':  # Windows
        PERSISTENT_DATA_ROOT = BASE_DIR / 'persistent_data'
    else:  # Linux/Production
        # Default to a persistent path outside of backend to prevent loss during redeployments
        PERSISTENT_DATA_ROOT = '/home/ubuntu/agrobridge_data/'

    
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Console: 4xx/5xx and uncaught exceptions (runserver / gunicorn stderr)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# Maximum File Upload Limits (25 MB) for WhatsApp Media Templates
DATA_UPLOAD_MAX_MEMORY_SIZE = 26214400  # 25 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 26214400  # 25 MB
