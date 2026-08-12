"""Base settings shared by all environments."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-do-not-use-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    # Local
    "apps.core",
    "apps.directory",
    "apps.grievances",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="bizedge"),
        "USER": env("POSTGRES_USER", default="bizedge"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="bizedge"),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Swappable platform models
#
# The grievances module is being built standalone but will later be merged into
# the wider BizEdge / MAKAY platform. Every reference to a model owned by the
# platform goes through one of these settings, in the same spirit as
# AUTH_USER_MODEL. Merging should be a settings change, not a rewrite.
#
# Do NOT import these models directly anywhere in apps.grievances. Use the
# helpers in apps.core.platform instead.
# ---------------------------------------------------------------------------
GRIEVANCES_EMPLOYEE_MODEL = env("GRIEVANCES_EMPLOYEE_MODEL", default="directory.Employee")
GRIEVANCES_DEPARTMENT_MODEL = env("GRIEVANCES_DEPARTMENT_MODEL", default="directory.Department")
GRIEVANCES_TRAINING_MODEL = env("GRIEVANCES_TRAINING_MODEL", default="directory.Training")
GRIEVANCES_ORGANISATION_MODEL = env("GRIEVANCES_ORGANISATION_MODEL", default="core.Organisation")

# Attachment limits — grievance evidence is sensitive, keep storage private.
GRIEVANCES_MAX_ATTACHMENT_BYTES = env.int("GRIEVANCES_MAX_ATTACHMENT_BYTES", default=25 * 1024 * 1024)
# Checked alongside the MIME type. The browser-supplied content_type header is
# not evidence of anything -- a renamed executable will happily claim to be a
# PDF -- so both must line up.
GRIEVANCES_ALLOWED_ATTACHMENT_EXTENSIONS = [
    ".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx",
]
GRIEVANCES_ALLOWED_ATTACHMENT_TYPES = [
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "BizEdge Grievances API",
    "DESCRIPTION": "Complaints, investigations and resolutions.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # `visibility` and `visibility_requested` share one choice set. Without an
    # override, generated clients get two identical enums with different names.
    # Several fields share one choice set -- `visibility`/`visibility_requested`
    # and `state`/`from_state`/`to_state`. Without these overrides, generated
    # clients get duplicate enums under different names.
    "ENUM_NAME_OVERRIDES": {
        "VisibilityEnum": "apps.grievances.enums.Visibility.choices",
        "ComplaintStateEnum": "apps.grievances.enums.ComplaintState.choices",
    },
    "COMPONENT_SPLIT_REQUEST": True,
}

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
