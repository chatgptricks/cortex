from app.main import app


def test_promos_api_contract_is_registered():
    paths = {route.path for route in app.routes}
    assert "/api/admin/promos" in paths
    assert "/api/admin/promos/{account}/{shortcode}" in paths
    assert "/api/admin/promos/backfill" in paths
    assert "/api/admin/promos/jobs/{job_id}" in paths
