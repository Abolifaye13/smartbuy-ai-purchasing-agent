import os
import tempfile
from fastapi.testclient import TestClient

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_PATH"] = tmp.name

from app.main import app, SCENARIO  # noqa: E402
client = TestClient(app)


def reset():
    client.post("/api/dev/reset")


def test_investigation_modifies_800_unit_recommendation():
    reset()
    r = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800})
    assert r.status_code == 200
    d = r.json()
    assert d["decision"] in {"MODIFY", "INVESTIGATE"}
    assert d["calculation"]["net_need_after_inventory_and_open_po"] == 380
    assert d["recommended_quantity"] <= 500  # storage is the tightest cap in defaults
    assert d["recommended_quantity"] % 100 == 0


def test_open_po_and_inventory_reduce_need():
    reset()
    r = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800})
    c = r.json()["calculation"]
    assert c["gross_need_forecast_plus_safety_stock"] == 700
    assert c["net_need_after_inventory_and_open_po"] == 380


def test_missing_data_escalates_to_investigate():
    reset()
    SCENARIO["data_complete"] = False
    r = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800})
    assert r.json()["decision"] == "INVESTIGATE"
    reset()


def test_approval_required():
    reset()
    r = client.post("/api/execute", json={"sku":"SKU-APPLE-1","node":"Nagpur FC-01",
        "supplier_id":"SUP-APPLE-01","quantity":400,"approved":False})
    assert r.status_code == 400


def test_successful_po_is_persisted_and_validated():
    reset()
    d = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800}).json()
    q = d["recommended_quantity"]
    r = client.post("/api/execute", json={"sku":"SKU-APPLE-1","node":"Nagpur FC-01",
        "supplier_id":"SUP-APPLE-01","quantity":q,"approved":True})
    assert r.status_code == 200
    result = r.json()
    assert result["validation"]["passed"] is True
    assert all(result["validation"]["checks"].values())
    persisted = client.get("/api/purchase-orders").json()["purchase_orders"]
    assert any(p["id"] == result["purchase_order"]["id"] for p in persisted)


def test_budget_change_blocks_stale_recommendation():
    reset()
    d = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800}).json()
    q = d["recommended_quantity"]
    client.post("/api/dev/mutate-scenario", json={"budget_available":100})
    r = client.post("/api/execute", json={"sku":"SKU-APPLE-1","node":"Nagpur FC-01",
        "supplier_id":"SUP-APPLE-01","quantity":q,"approved":True})
    assert r.status_code == 409
    reset()


def test_storage_constraint_changes_recommended_quantity():
    reset()
    client.post("/api/dev/mutate-scenario", json={"storage_capacity_free":200})
    d = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800}).json()
    assert d["recommended_quantity"] <= 200
    assert d["recommended_quantity"] % 100 == 0
    reset()


def test_invalid_sku_rejected():
    r = client.post("/api/investigate", json={"sku":"NOT-A-SKU","proposed_quantity":800})
    assert r.status_code == 404


def test_constraint_explanation_present():
    reset()
    d = client.post("/api/investigate", json={"sku":"SKU-APPLE-1","proposed_quantity":800}).json()
    assert "evidence" in d and "constraints" in d and "explanation" in d
    assert d["calculation"]["recommended_total_cost"] <= d["evidence"]["budget_available"]
