async def test_mocks_are_reachable_in_process(mocks_http):
    resp = await mocks_http.get("/_admin/scenarios")
    assert resp.status_code == 200
    assert resp.json()["active"] == "clean"
