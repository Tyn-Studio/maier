import pytest


@pytest.mark.django_db
def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_home_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Culler" in response.content
