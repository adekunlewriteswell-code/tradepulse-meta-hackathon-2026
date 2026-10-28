from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.main import create_app


def test_health_and_root(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.get("/").status_code == 200
        assert client.get("/").json()["ai_mode"] == "mock"
        assert app.state.database is not None
        # Verify the public HTTP surface, independent of FastAPI's lazy router internals.
        schema_response = client.get('/openapi.json')
        assert schema_response.status_code == 200
        paths = schema_response.json()['paths']
        assert set(paths) == {'/', '/health'}
        assert all(set(operations) == {'get'} for operations in paths.values())
        for path in ('/docs', '/docs/oauth2-redirect', '/redoc'):
            assert client.get(path).status_code == 200
        for path in ('/missions', '/release', '/_test/ai'):
            assert client.get(path).status_code == 404


async def test_lifespan_creates_schema_on_first_run(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.database.engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "retailers" in tables
            assert "audit_logs" in tables


def test_app_instances_do_not_share_clients(settings):
    assert create_app(settings).state.ai_client is not create_app(settings).state.ai_client
