import os
import sys

_MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MCP_DIR)
sys.path.insert(0, os.path.dirname(_MCP_DIR))
os.environ.setdefault("SLVD_ENGINE_URL", "http://localhost:1")  # no engine in tests

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server as mcp_server  # noqa: E402

app = mcp_server.app


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "credit-memo-mcp"
    assert r.json()["cases"] == 3


def test_governed_tool_ok(client):
    r = client.post("/tools/get_crm_profile", json={"case_id": "CR-2026-00451"},
                    headers={"Authorization": "Bearer nick"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "get_crm_profile"
    assert body["data"]["beneficial_ownership_update"] == "pending"


def test_governed_tool_unauthorized_user(client):
    r = client.post("/tools/get_crm_profile", json={"case_id": "CR-2026-00451"},
                    headers={"Authorization": "Bearer mallory"})
    assert r.status_code == 403


def test_governed_tool_missing_token(client):
    r = client.post("/tools/get_crm_profile", json={"case_id": "CR-2026-00451"})
    assert r.status_code == 401


def test_governed_tool_unknown_case(client):
    r = client.post("/tools/get_crm_profile", json={"case_id": "CR-0000-00000"},
                    headers={"Authorization": "Bearer nick"})
    assert r.status_code == 404


def test_all_five_data_tools(client):
    for tool in ["get_crm_profile", "get_credit_exposure", "get_transactions",
                 "get_compliance_status", "get_prior_memo"]:
        r = client.post(f"/tools/{tool}", json={"case_id": "CR-2026-00452"},
                        headers={"Authorization": "Bearer nick"})
        assert r.status_code == 200, tool
        assert r.json()["source"] == tool


def test_submit_tool(client):
    r = client.post("/tools/workflow__submit_credit_memo",
                    json={"case_id": "CR-2026-00451", "memo_md": "# memo"},
                    headers={"Authorization": "Bearer nick"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "received"


def test_mcp_initialize_and_tools_list(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}})
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "credit-memo-mcp"
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r2.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "get_crm_profile" in names and "workflow__submit_credit_memo" in names
    assert len(tools) == 6


def test_mcp_tools_call_ok(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "get_credit_exposure",
                                              "arguments": {"case_id": "CR-2026-00453"}}},
                    headers={"X-User-Id": "nick"})
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is False
    assert "risk_rating" in r.json()["result"]["content"][0]["text"]


def test_mcp_tools_call_denied(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "get_crm_profile",
                                              "arguments": {"case_id": "CR-2026-00451"}}},
                    headers={"X-User-Id": "mallory"})
    assert r.json()["result"]["isError"] is True
