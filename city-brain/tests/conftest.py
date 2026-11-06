import pytest
from fastapi.testclient import TestClient

from city_brain.main import app


@pytest.fixture
def client():
    return TestClient(app)
