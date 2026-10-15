.PHONY: check test live lint docs

check: lint test

lint:
	ruff check .
	ruff format --check .
	mypy

test:
	pytest

# Contrat contre la vraie API (identifiants dans l'environnement).
live:
	pytest -m live

# Site Swagger UI + Redoc dans _site/, servi sur http://localhost:8000
docs:
	rm -rf _site && mkdir _site
	cp docs/*.html _site/ && cp openapi/myges.yaml _site/openapi.yaml
	python -m http.server -d _site 8000
