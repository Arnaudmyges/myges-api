#!/usr/bin/env bash
# Publie une nouvelle version : contrôles, commit + tag, publication PyPI, push.
#
# Usage : scripts/release.sh 0.4.0
# Prérequis : PYPI_TOKEN dans .env (jeton PyPI du projet, jamais commité).
set -euo pipefail

VERSION=${1:?usage : $0 <version, ex. 0.4.0>}
cd "$(dirname "$0")/.."
PY=.venv/bin/python

[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version invalide : $VERSION" >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo "Modifications non commitées : commiter d'abord." >&2; exit 1; }
[[ $(git branch --show-current) == main ]] || { echo "À lancer depuis main." >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && { echo "Tag v$VERSION déjà présent." >&2; exit 1; }
token=$(grep '^PYPI_TOKEN=' .env | cut -d= -f2- | tr -d "\"' \r")
[[ $token == pypi-* ]] || { echo "PYPI_TOKEN absent ou invalide dans .env." >&2; exit 1; }

sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" src/myges/_version.py
$PY -m ruff check .
$PY -m mypy
$PY -m pytest -q

git commit -qam "Version $VERSION"
git tag -a "v$VERSION" -m "Version $VERSION"

# Construit depuis le commit lui-même : aucun fichier local non suivi (.env…) ne peut
# finir dans l'archive.
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
git archive "v$VERSION" | tar -x -C "$build"
$PY -m build -q -o "$build/dist" "$build"
$PY -m twine check "$build"/dist/*
TWINE_USERNAME=__token__ TWINE_PASSWORD=$token \
    $PY -m twine upload --non-interactive --disable-progress-bar "$build"/dist/*

git push -q
git push -q origin "v$VERSION"
echo "Version $VERSION publiée : https://pypi.org/project/myges-api/$VERSION/"
