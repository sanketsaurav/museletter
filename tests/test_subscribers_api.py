from conftest import AUTH, add_subscriber


async def test_default_list_exists(app_client):
    app, client = app_client
    resp = await client.get("/v1/lists", headers=AUTH)
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["lists"]]
    assert slugs == ["default"]


async def test_requires_api_key(app_client):
    app, client = app_client
    assert (await client.get("/v1/lists")).status_code == 401
    assert (await client.get("/v1/lists", headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_add_list_and_get_subscriber(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "Ada@Example.com", name="Ada", tags=["vip"])
    assert sub["email"] == "ada@example.com"
    assert sub["status"] == "active"
    assert sub["tags"] == ["vip"]

    dup = await client.post("/v1/lists/default/subscribers", json={"email": "ada@example.com"}, headers=AUTH)
    assert dup.status_code == 409

    bad = await client.post("/v1/lists/default/subscribers", json={"email": "nope"}, headers=AUTH)
    assert bad.status_code == 422

    listing = await client.get("/v1/lists/default/subscribers?tag=vip", headers=AUTH)
    assert listing.json()["total"] == 1

    resp = await client.delete(f"/v1/subscribers/{sub['id']}", headers=AUTH)
    assert resp.status_code == 204
    assert (await client.get(f"/v1/subscribers/{sub['id']}", headers=AUTH)).status_code == 404


async def test_csv_import_and_export(app_client):
    app, client = app_client
    csv_text = "email,name,tags,status\na@x.com,Alice,vip;beta,active\nb@x.com,Bob,,active\nbad-email,,,\na@x.com,Dup,,active\n"
    resp = await client.post("/v1/lists/default/subscribers/import", json={"csv": csv_text}, headers=AUTH)
    data = resp.json()
    assert data == {"imported": 2, "skipped_existing": 1, "skipped_invalid": 1}

    headerless = "c@x.com,Carol\nd@x.com\n"
    resp = await client.post("/v1/lists/default/subscribers/import", json={"csv": headerless}, headers=AUTH)
    assert resp.json()["imported"] == 2

    export = await client.get("/v1/lists/default/subscribers/export", headers=AUTH)
    assert "a@x.com,Alice,active,beta;vip" in export.text
    assert "c@x.com,Carol" in export.text


async def test_multiple_lists(app_client):
    app, client = app_client
    resp = await client.post("/v1/lists", json={"name": "Beta Readers"}, headers=AUTH)
    assert resp.status_code == 201
    assert resp.json()["slug"] == "beta-readers"

    await add_subscriber(client, "x@y.com")
    other = await client.post("/v1/lists/beta-readers/subscribers", json={"email": "x@y.com"}, headers=AUTH)
    assert other.status_code == 201, "same email may exist on two lists"

    last = await client.delete("/v1/lists/beta-readers", headers=AUTH)
    assert last.status_code == 204
    only = await client.delete("/v1/lists/default", headers=AUTH)
    assert only.status_code == 409
