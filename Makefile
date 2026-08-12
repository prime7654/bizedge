# Common tasks. Everything runs inside the web container so behaviour matches
# CI and production rather than whatever happens to be on your machine.

DC := docker compose
RUN := $(DC) exec -T web

.PHONY: help up down logs shell migrate migrations test check docs docs-check seed

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start the stack
	$(DC) up -d

down:  ## Stop the stack
	$(DC) down

logs:  ## Tail the web container
	$(DC) logs -f web

shell:  ## Django shell
	$(DC) exec web python manage.py shell

migrate:  ## Apply migrations
	$(RUN) python manage.py migrate

migrations:  ## Create migrations
	$(RUN) python manage.py makemigrations

test:  ## Run the test suite
	$(RUN) pytest

check:  ## Django system checks
	$(RUN) python manage.py check

docs:  ## Regenerate the API schema and standalone HTML reference
	$(RUN) python scripts/build_api_docs.py

docs-check:  ## Fail if the committed schema is out of date (for CI)
	@$(RUN) python manage.py spectacular --file /tmp/schema-check.yml >/dev/null 2>&1
	@$(RUN) diff -q /tmp/schema-check.yml docs/api/schema.yml >/dev/null \
	 || (echo "docs/api/schema.yml is stale -- run 'make docs' and commit"; exit 1)
	@echo "schema is up to date"

seed:  ## Load demo data
	$(RUN) python manage.py seed_demo
