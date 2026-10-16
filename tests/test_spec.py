"""La spec est valide, complète, et les fixtures s'y conforment."""

from __future__ import annotations

import copy

import pytest
from openapi_spec_validator import validate

from conftest import FIXTURES, fixture
from contract import operations, schema_errors, spec

FIXTURE_OPS = sorted(p.stem for p in FIXTURES.glob("get*.json"))


def test_spec_is_valid_openapi_31() -> None:
    validate(spec())


@pytest.mark.parametrize("operation_id", sorted(operations()))
def test_operation_is_documented(operation_id: str) -> None:
    method, path = operations()[operation_id]
    op = spec()["paths"][path][method]
    assert op.get("tags"), "tag manquant"
    assert op.get("summary"), "summary manquant"
    assert "x-verified" in op, "x-verified manquant"
    assert "401" in op["responses"] or path == "/oauth/authorize"


def test_every_read_operation_has_a_fixture() -> None:
    binary = {"authorize", "downloadCourseFile", "downloadProjectFile"}
    reads = {op for op, (method, _) in operations().items() if method == "get"} - binary
    assert reads == set(FIXTURE_OPS)


@pytest.mark.parametrize("operation_id", FIXTURE_OPS)
def test_fixture_matches_schema(operation_id: str) -> None:
    assert schema_errors(operation_id, 200, fixture(operation_id)) == []


@pytest.mark.parametrize(("name", "status"), [("error_404", 404), ("error_400", 400)])
def test_error_fixtures_match_schema(name: str, status: int) -> None:
    assert schema_errors("getCourse" if status == 404 else "getAgenda", status, fixture(name)) == []


def test_validator_detects_type_drift() -> None:
    body = copy.deepcopy(fixture("getCourses"))
    body["result"][0]["rc_id"] = "500001"
    assert schema_errors("getCourses", 200, body) == ["$.result[0].rc_id: type"]


def test_validator_never_leaks_values() -> None:
    body = copy.deepcopy(fixture("getProfile"))
    body["result"]["email"] = 12345678
    errors = schema_errors("getProfile", 200, body)
    assert errors
    assert all("12345678" not in e for e in errors)
