.PHONY: check test live lint docs release

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

# Swagger UI + Redoc sur http://localhost:8000/docs/ (même arborescence que GitHub Pages)
docs:
	python -m http.server 8000

# Nouvelle version sur PyPI : make release VERSION=0.4.0
release:
	scripts/release.sh $(VERSION)
