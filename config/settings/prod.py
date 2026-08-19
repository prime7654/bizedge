"""Production and staging settings."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Render injects its generated hostname here. Adding it explicitly means the
# service works on first boot without anyone having to guess the domain.
# env.list, not env: base.py already declares ALLOWED_HOSTS as a list type, so
# env() returns a list and calling .split() on it raises at import time.
ALLOWED_HOSTS = [h.strip() for h in env.list("ALLOWED_HOSTS", default=[]) if h.strip()]

RENDER_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME", default=None)
if RENDER_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_HOSTNAME)

RAILWAY_HOSTNAME = env("RAILWAY_PUBLIC_DOMAIN", default=None)
if RAILWAY_HOSTNAME:
    ALLOWED_HOSTS.append(RAILWAY_HOSTNAME)

if not ALLOWED_HOSTS:
    # Fail loudly rather than serving every Host header. An empty list with
    # DEBUG=False rejects everything anyway -- this says why.
    raise RuntimeError(
        "No ALLOWED_HOSTS configured. Set ALLOWED_HOSTS, or deploy somewhere "
        "that provides RENDER_EXTERNAL_HOSTNAME / RAILWAY_PUBLIC_DOMAIN."
    )

# Django requires the scheme here, unlike ALLOWED_HOSTS. Without it the admin
# login and any session-authenticated POST fail CSRF verification over HTTPS.
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS if host != "*"]
# The frontend posts from a different site (Vercel), so its origin must be a
# trusted CSRF origin too -- CSRF checks the request's Origin, which is the
# frontend, not this API's host. Reuse the CORS allow-list from base settings.
CSRF_TRUSTED_ORIGINS += [o for o in CORS_ALLOWED_ORIGINS if o.startswith("https://")]

# The platform terminates TLS and forwards over HTTP, so Django needs telling
# the original request was secure -- otherwise SECURE_SSL_REDIRECT loops.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# The frontend and API are on different sites (vercel.app vs onrender.com), so
# the session and CSRF cookies must be SameSite=None to be sent on cross-site
# requests at all. None requires Secure, set just above. Without this, CORS is
# configured correctly and the browser still never sends the session cookie.
SESSION_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SAMESITE = "None"
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"level": "ERROR", "propagate": True},
        # Notifications only log today, so keep them visible in staging --
        # it is the only way to see that they fired.
        "apps.grievances.notifications": {"level": "INFO", "propagate": True},
    },
}
