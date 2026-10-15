import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
CONFIG = {"Authorization": "Bearer config-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
# Dev patient user is linked to Patient/pat-1 (see app.security DEFAULT_DEV_TOKENS).
PATIENT = {"Authorization": "Bearer patient-token"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c
