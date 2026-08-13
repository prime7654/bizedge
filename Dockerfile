FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
# Dev requirements include pytest, which staging does not need but which makes
# it possible to run the suite against the deployed database if something only
# reproduces there. Drop to requirements.txt for a real production image.
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

# git does not always preserve the executable bit on checkout, so set it here
# rather than relying on the repo.
RUN chmod +x /app/scripts/*.sh

# Collected at build time so the web process starts serving immediately.
# DEBUG and a dummy key are set only for this command -- collectstatic does not
# touch the database, and prod settings refuse to import without a host.
RUN DJANGO_SETTINGS_MODULE=config.settings.dev SECRET_KEY=build-only \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Compose overrides this for local development. Render supplies its own command
# via render.yaml, which also runs migrations first.
CMD ["/app/scripts/start.sh"]
