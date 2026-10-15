from pathlib import Path
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DB_PATH = os.getenv("DATABASE_PATH", str(ROOT / "smartbuy_assignment.db"))
app = FastAPI(title="SmartBuy AI Procurement Agent", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, node TEXT NOT NULL,
            supplier_id TEXT NOT NULL, quantity INTEGER NOT NULL, unit_cost REAL NOT NULL,
            total_cost REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            validation_json TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
            details_json TEXT NOT NULL, created_at TEXT NOT NULL)""")


init_db()

# Mutable in-memory mock services. Reset by restarting the server.
SCENARIO = {
    "sku": "SKU-APPLE-1", "product_name": "Apples — 1 kg crate", "node": "Nagpur FC-01",
    "on_hand": 220, "forecast_demand": 620, "safety_stock": 80,
    "open_po_units": 100, "open_po_id": "PO-OPEN-1042", "open_po_status": "open",
    "supplier_id": "SUP-APPLE-01", "supplier_name": "FreshFields Produce",
    "unit_cost": 85.0, "supplier_lead_time_days": 3, "supplier_moq": 100,
    "supplier_capacity": 700, "budget_available": 40000, "storage_capacity_free": 500,
    "proposed_quantity": 800, "horizon_days": 7,
    "data_complete": True
}


def audit(event: str, details: dict[str, Any]):
    with db() as c:
        c.execute("INSERT INTO audit_log(event, details_json, created_at) VALUES(?,?,?)",
                  (event, json.dumps(details), now()))


class InvestigateRequest(BaseModel):
    sku: str = "SKU-APPLE-1"
    proposed_quantity: int = Field(default=800, ge=0, le=100000)


class ExecuteRequest(BaseModel):
    sku: str
    node: str
    supplier_id: str
    quantity: int = Field(gt=0, le=100000)
    approved: bool


class ScenarioMutation(BaseModel):
    budget_available: float | None = Field(default=None, ge=0)
    storage_capacity_free: int | None = Field(default=None, ge=0)
    supplier_capacity: int | None = Field(default=None, ge=0)
    on_hand: int | None = Field(default=None, ge=0)
    forecast_demand: int | None = Field(default=None, ge=0)


def snapshot() -> dict:
    return dict(SCENARIO)


def compute_decision(s: dict, proposed: int | None = None) -> dict:
    required_keys = ["on_hand", "forecast_demand", "safety_stock", "open_po_units",
                     "supplier_moq", "supplier_capacity", "budget_available", "storage_capacity_free",
                     "unit_cost", "supplier_lead_time_days"]
    missing = [k for k in required_keys if s.get(k) is None]
    if not s.get("data_complete", True) or missing:
        return {"decision": "INVESTIGATE", "reason": "Required purchasing information is incomplete.",
                "missing_information": missing, "evidence": s, "constraints": [], "calculation": {}}

    gross_need = s["forecast_demand"] + s["safety_stock"]
    net_need = max(0, gross_need - s["on_hand"] - s["open_po_units"])
    moq = s["supplier_moq"]
    desired = max(net_need, moq) if net_need > 0 else 0
    # Round positive target up to MOQ multiple; common supplier pack-size assumption.
    if desired > 0 and moq > 0:
        desired = ((desired + moq - 1) // moq) * moq

    max_by_budget = int(s["budget_available"] // s["unit_cost"]) if s["unit_cost"] > 0 else 0
    max_feasible = min(s["supplier_capacity"], s["storage_capacity_free"], max_by_budget)
    feasible = min(desired, max_feasible)
    if feasible > 0 and moq > 0 and feasible < moq:
        feasible = 0
    elif feasible > 0 and moq > 0:
        feasible = (feasible // moq) * moq

    constraints = []
    if desired > s["supplier_capacity"]:
        constraints.append({"constraint": "supplier_capacity", "limit": s["supplier_capacity"],
                            "desired": desired, "impact": "Quantity capped by supplier capacity"})
    if desired > s["storage_capacity_free"]:
        constraints.append({"constraint": "storage_capacity", "limit": s["storage_capacity_free"],
                            "desired": desired, "impact": "Quantity capped by free storage"})
    if desired > max_by_budget:
        constraints.append({"constraint": "budget", "limit": max_by_budget,
                            "desired": desired, "impact": "Quantity capped by available budget"})
    if 0 < feasible < net_need:
        constraints.append({"constraint": "coverage_gap", "limit": feasible,
                            "desired": net_need, "impact": "Feasible order may not cover full calculated need"})
    if net_need == 0:
        decision = "REJECT"
        action_qty = 0
        reason = "On-hand inventory plus open purchase orders already cover forecast demand and safety stock."
    elif feasible == 0:
        decision = "INVESTIGATE"
        action_qty = 0
        reason = "No positive order quantity satisfies all hard constraints and supplier MOQ."
    elif proposed is not None and proposed == feasible and not constraints and proposed >= net_need:
        decision = "ACCEPT"
        action_qty = feasible
        reason = "The proposed quantity matches calculated need and all purchasing constraints."
    elif feasible < net_need:
        decision = "INVESTIGATE"
        action_qty = feasible
        reason = "A constrained partial order is possible, but the remaining coverage gap needs buyer review."
    else:
        decision = "MODIFY"
        action_qty = feasible
        reason = "The proposed quantity differs from the calculated feasible replenishment quantity."
    if proposed is not None and proposed > 0 and proposed > max_feasible:
        if feasible == 0:
            decision = "INVESTIGATE"
        elif feasible >= net_need:
            decision = "MODIFY"
    calc = {
        "gross_need_forecast_plus_safety_stock": gross_need,
        "net_need_after_inventory_and_open_po": net_need,
        "desired_order_after_moq_rounding": desired,
        "max_by_budget": max_by_budget,
        "max_feasible_before_moq_rounding": max_feasible,
        "recommended_quantity": action_qty,
        "recommended_total_cost": round(action_qty * s["unit_cost"], 2),
        "remaining_coverage_gap": max(0, net_need - action_qty)
    }
    return {
        "decision": decision, "reason": reason, "proposed_quantity": proposed,
        "recommended_quantity": action_qty, "calculation": calc, "constraints": constraints,
        "evidence": s,
        "explanation": [
            f"Forecast demand is {s['forecast_demand']} units over {s['horizon_days']} days.",
            f"Safety stock is {s['safety_stock']} units.",
            f"On-hand stock is {s['on_hand']} and open PO supply is {s['open_po_units']} units.",
            f"Net replenishment need is {net_need} units before supplier/budget/storage limits.",
            f"Budget supports up to {max_by_budget} units at {s['unit_cost']:.2f} per unit.",
            f"Free storage supports {s['storage_capacity_free']} units; supplier capacity is {s['supplier_capacity']}."
        ]
    }


def validate_po(po: dict, s: dict) -> dict:
    checks = {
        "quantity_positive": po["quantity"] > 0,
        "supplier_moq_respected": po["quantity"] >= s["supplier_moq"] and po["quantity"] % s["supplier_moq"] == 0,
        "supplier_capacity_respected": po["quantity"] <= s["supplier_capacity"],
        "budget_respected": po["total_cost"] <= s["budget_available"],
        "storage_respected": po["quantity"] <= s["storage_capacity_free"],
        "sku_matches": po["sku"] == s["sku"],
        "node_matches": po["node"] == s["node"],
        "supplier_matches": po["supplier_id"] == s["supplier_id"],
        "persisted_status_confirmed": po["status"] == "CONFIRMED"
    }
    return {"passed": all(checks.values()), "checks": checks}


@app.get("/", response_class=HTMLResponse)
def home():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "SmartBuy AI Procurement Agent"}


@app.get("/api/scenario")
def get_scenario():
    return snapshot()


@app.post("/api/investigate")
def investigate(req: InvestigateRequest):
    s = snapshot()
    if req.sku != s["sku"]:
        raise HTTPException(404, "SKU not found in mock purchasing system")
    result = compute_decision(s, req.proposed_quantity)
    audit("investigation_completed", {"sku": req.sku, "decision": result["decision"],
                                      "recommended_quantity": result.get("recommended_quantity")})
    return result


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    if not req.approved:
        raise HTTPException(400, "Buyer approval is required before executing a purchase order")
    s = snapshot()
    if req.sku != s["sku"] or req.node != s["node"] or req.supplier_id != s["supplier_id"]:
        raise HTTPException(409, "Execution details do not match the current scenario")
    # Feedback loop: re-investigate current state immediately before action.
    latest = compute_decision(s, req.quantity)
    if latest["decision"] == "INVESTIGATE" and latest["recommended_quantity"] == 0:
        audit("execution_blocked", {"reason": latest["reason"], "request": req.model_dump()})
        raise HTTPException(409, "Execution blocked by current constraints: " + latest["reason"])
    calc = latest.get("calculation", {})
    if req.quantity != latest.get("recommended_quantity"):
        audit("execution_blocked", {"reason": "quantity no longer matches current recommendation",
                                    "requested": req.quantity, "recommended": latest.get("recommended_quantity")})
        raise HTTPException(409, f"State changed or quantity mismatch. Current recommended quantity is {latest.get('recommended_quantity')}. Re-investigate.")
    total = round(req.quantity * s["unit_cost"], 2)
    # Insert as pending, read back, validate, then confirm or mark failed.
    with db() as c:
        cur = c.execute("""INSERT INTO purchase_orders(sku,node,supplier_id,quantity,unit_cost,total_cost,status,created_at,validation_json)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (req.sku, req.node, req.supplier_id, req.quantity, s["unit_cost"], total,
                         "PENDING_VALIDATION", now(), "{}"))
        po_id = cur.lastrowid
        row = c.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        po = dict(row)
        validation = validate_po(po, s)
        status = "CONFIRMED" if validation["passed"] else "VALIDATION_FAILED"
        c.execute("UPDATE purchase_orders SET status=?, validation_json=? WHERE id=?",
                  (status, json.dumps(validation), po_id))
        row2 = c.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        persisted = dict(row2)
        persisted["validation"] = json.loads(persisted["validation_json"])
    audit("purchase_order_validated", {"po_id": po_id, "validation": validation, "status": status})
    if not validation["passed"]:
        raise HTTPException(500, f"PO {po_id} failed post-action validation. It was flagged for review.")
    return {"purchase_order": persisted, "validation": validation,
            "message": "Mock PO created, read back from database, and validated successfully.",
            "note": "No real supplier was contacted and no money was transferred."}


@app.get("/api/purchase-orders")
def list_pos():
    with db() as c:
        rows = c.execute("SELECT * FROM purchase_orders ORDER BY id DESC LIMIT 30").fetchall()
    return {"purchase_orders": [dict(r) | {"validation": json.loads(r["validation_json"])} for r in rows]}


@app.get("/api/audit")
def get_audit():
    with db() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    return {"events": [dict(r) | {"details": json.loads(r["details_json"])} for r in rows]}


@app.post("/api/dev/mutate-scenario")
def mutate_scenario(req: ScenarioMutation):
    """Demo-only environment mutation for testing the feedback loop."""
    changes = req.model_dump(exclude_none=True)
    SCENARIO.update(changes)
    audit("scenario_mutated_for_test", changes)
    return {"scenario": snapshot(), "message": "Mock environment updated for this running process."}


@app.post("/api/dev/reset")
def reset_scenario():
    defaults = {
        "on_hand": 220, "forecast_demand": 620, "safety_stock": 80, "open_po_units": 100,
        "supplier_capacity": 700, "budget_available": 40000, "storage_capacity_free": 500,
        "proposed_quantity": 800, "data_complete": True
    }
    SCENARIO.update(defaults)
    audit("scenario_reset", defaults)
    return {"scenario": snapshot()}
