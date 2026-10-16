"""CORS regression tests.

Found live 2026-10-15: the backend shipped with NO CORS middleware, so
the web app in a real browser could never read API responses (the
browser blocks cross-origin responses even when the API is up) while
every server-side smoke stayed green. Browser E2E was NOT RUN in the
phase SQA reports — this is the class of bug that hid behind that gap.
"""
from tests.conftest import ADMIN

DEV_ORIGIN = "http://localhost:5173"


def test_preflight_from_dev_origin_allowed(client):
    r = client.options(
        "/fhir/Patient",
        headers={
            "Origin": DEV_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN


def test_actual_response_carries_cors_header(client):
    r = client.get("/health", headers={"Origin": DEV_ORIGIN})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN


def test_unknown_origin_not_allowed(client):
    r = client.options(
        "/fhir/Patient",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-origin") != "http://evil.example"
