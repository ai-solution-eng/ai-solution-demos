import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state.export_jobs import EXPORT_JOBS
from app.state.rooms import ROOMS


@pytest.fixture(autouse=True)
def reset_in_memory_state():
    ROOMS.clear()
    EXPORT_JOBS.clear()
    yield
    ROOMS.clear()
    EXPORT_JOBS.clear()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
