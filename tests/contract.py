"""Validation d'une réponse JSON contre le schéma déclaré dans la spec OpenAPI.

Les erreurs sont rapportées par chemin JSON et mot-clé de schéma, jamais avec
la valeur fautive : les tests « live » tournent sur de vraies données
personnelles, qui ne doivent pas finir dans des logs de CI.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "myges.yaml"


@cache
def spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _deref(node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    target: Any = spec()
    for part in ref.removeprefix("#/").split("/"):
        target = target[part.replace("~1", "/").replace("~0", "~")]
    return _deref(target)


def operations() -> dict[str, tuple[str, str]]:
    """operationId -> (méthode, chemin)."""
    ops = {}
    for path, item in spec()["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op:
                ops[op["operationId"]] = (method, path)
    return ops


def response_schema_pointer(operation_id: str, status: int) -> str:
    method, path = operations()[operation_id]
    responses = spec()["paths"][path][method]["responses"]
    response = responses[str(status)]
    if "$ref" in response:
        base = response["$ref"]
        response = _deref(response)
    else:
        escaped = path.replace("~", "~0").replace("/", "~1")
        base = f"#/paths/{escaped}/{method}/responses/{status}"
    content = response.get("content")
    if not content:
        raise LookupError(f"{operation_id} {status}: pas de corps déclaré")
    media = next(m for m in content if "json" in m)
    return f"{base}/content/{media.replace('/', '~1')}/schema"


def schema_errors(operation_id: str, status: int, body: Any) -> list[str]:
    """Erreurs de validation, sous la forme « $.chemin: mot-clé »."""
    root = dict(spec())
    root["$ref"] = response_schema_pointer(operation_id, status)
    validator = Draft202012Validator(root)
    return sorted(f"{error.json_path}: {error.validator}" for error in validator.iter_errors(body))
