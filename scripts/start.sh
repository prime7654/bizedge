#!/bin/sh
# Container entrypoint for deployment.
#
# A script rather than an inline command in render.yaml: the inline form has to
# survive YAML folding and then Render's own shell handling, and getting that
# wrong fails at boot with a bare "not found" that says nothing useful. A file
# is unambiguous and can be run locally to check it.
set -e

echo "==> Running migrations"
python manage.py migrate --noinput

# Render sets PORT; default for anywhere that does not.
: "${PORT:=8000}"
# Render sets WEB_CONCURRENCY based on the instance size. Respect it -- on a
# free instance two workers will fight over the available memory.
: "${WEB_CONCURRENCY:=2}"

echo "==> Starting gunicorn on port $PORT with $WEB_CONCURRENCY worker(s)"
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:$PORT" \
    --workers "$WEB_CONCURRENCY" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
