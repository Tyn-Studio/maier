import pytest


@pytest.mark.django_db
def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_home_redirects_to_grid(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == "/grid"
