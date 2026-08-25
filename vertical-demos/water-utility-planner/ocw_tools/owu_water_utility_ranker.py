"""
title: Water Utility Ranker
author: Daniel Cao
version: 0.4.1
required_open_webui_version: 0.5.0
description: >
  Four tools that fetch pipe data from EzPresto (via MCP Streamable HTTP with
  session handshake + bearer JWT) and score/explain it via a Bento-packaged
  XGBoost model on MLIS.

  MCP transport implements: initialize -> Mcp-Session-Id -> tools/call
  with automatic session refresh on expiry.

Install:
  1. Open WebUI → Workspace → Tools → + → paste this file.
  2. Set valves (per-user or globally):
       EZPRESTO_MCP_URL     default already points at the in-cluster MCP server
       EZPRESTO_MCP_TOKEN   bearer JWT — copy from kagent/ezpresto-agent Secret
                            (see notes: expires ~1h after ezpresto-agent last rotated)
       EZPRESTO_SQL_TOOL    name of the SQL execution tool (default: execute_query)
       MLIS_ENDPOINT_URL    from MLIS UI after deploying the Bento
       MLIS_API_KEY         from MLIS UI when creating the endpoint
  3. In a chat, select deepseek-v4-flash-0731 (or gemma-4-31b-ab), enable all four
     tools: query_pipes, rank_pipes, explain_pipe, ezpresto_sql.

Token refresh:
  The EZPRESTO_MCP_TOKEN is a Kubernetes-projected service account JWT that
  expires ~1 hour after ezpresto-agent last rotated it. Refresh before demos:

    kubectl -n kagent get secret ezpresto-agent -o jsonpath='{.data.config\\.json}' \\
      | base64 -d \\
      | python3 -c "import sys, json; c=json.load(sys.stdin); \\
          print(c['http_tools'][0]['params']['headers']['Authorization'].replace('Bearer ',''))"

  If the Secret's token is stale, restart ezpresto-agent to force rotation:
    kubectl -n kagent rollout restart deploy/ezpresto-agent
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field


PIPES_TABLE = "waterutility.default.pipes"
WORK_ORDERS_TABLE = "waterutility.default.work_orders"

# Feature columns MLIS expects — must match Bento service.py
FEATURE_COLS = [
    "age_years", "material_ordinal", "diameter_mm", "slope_pct", "depth_m",
    "nearest_tree_dist_m", "trees_within_15m", "dominant_species_riskscore",
    "historical_incident_count",
]

# Columns returned to the LLM for display context (superset of FEATURE_COLS)
DISPLAY_COLS = [
    "pipe_id", "district_code", "material", "year_installed",
    "age_years", "diameter_mm", "depth_m",
    "nearest_tree_dist_m", "dominant_species", "trees_within_15m",
    "historical_incident_count", "soil_class", "traffic_load_class",
]


class Tools:
    class Valves(BaseModel):
        EZPRESTO_MCP_URL: str = Field(
            default="http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp",
            description="EzPresto MCP endpoint. Cluster-internal DNS.",
        )
        EZPRESTO_MCP_TOKEN: str = Field(
            default="",
            description="Bearer JWT for the MCP server. Copy from kagent/ezpresto-agent "
                        "Secret's config.json (http_tools[0].params.headers.Authorization). "
                        "Expires ~1 hour after ezpresto-agent last rotated its token.",
        )
        EZPRESTO_SQL_TOOL: str = Field(
            default="execute_query",
            description="Name of the SQL execution tool on the MCP server. "
                        "Verify with tools/list if unsure.",
        )
        MLIS_ENDPOINT_URL: str = Field(
            default="https://water-utility-ranker.mlis.example.com",
            description="MLIS endpoint for the deployed Bento (no trailing slash).",
        )
        MLIS_API_KEY: str = Field(
            default="",
            description="MLIS API key. Copy from the MLIS endpoint details in the AIE UI.",
        )
        TIMEOUT_SECONDS: int = Field(default=20)
        VERIFY_TLS: bool = Field(default=True)

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True
        self._session_id: Optional[str] = None

    # ---- helpers ---------------------------------------------------------

    def _mcp_new_session(self, headers_base: dict) -> str:
        """Perform MCP initialize handshake, return the session id."""
        init_payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "owu-water-utility-ranker", "version": "0.4.0"},
            },
        }
        r = requests.post(
            self.valves.EZPRESTO_MCP_URL,
            json=init_payload,
            headers=headers_base,
            timeout=self.valves.TIMEOUT_SECONDS,
            verify=self.valves.VERIFY_TLS,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"MCP initialize failed status={r.status_code} body={r.text[:400]!r}"
            )
        session_id = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError(
                f"MCP initialize returned no Mcp-Session-Id header. "
                f"Headers: {dict(r.headers)}"
            )
        # Per MCP spec: send notifications/initialized after initialize
        try:
            requests.post(
                self.valves.EZPRESTO_MCP_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={**headers_base, "Mcp-Session-Id": session_id},
                timeout=self.valves.TIMEOUT_SECONDS,
                verify=self.valves.VERIFY_TLS,
            )
        except Exception:
            pass  # notification is fire-and-forget
        self._session_id = session_id
        return session_id

    def _mcp_call(self, tool_name: str, arguments: dict) -> Any:
        """Streamable HTTP MCP client with session handshake.

        Servers implementing MCP Streamable HTTP require:
          1. POST initialize -> capture Mcp-Session-Id from response header
          2. POST tools/call with Mcp-Session-Id header on every subsequent request
        """
        headers_base = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        token = self.valves.EZPRESTO_MCP_TOKEN
        if token:
            headers_base["Authorization"] = f"Bearer {token}"

        # Ensure we have a session id (create one on first call, reuse across calls)
        if not getattr(self, "_session_id", None):
            self._mcp_new_session(headers_base)

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        def _do_call(session_id: str):
            call_headers = {**headers_base, "Mcp-Session-Id": session_id}
            return requests.post(
                self.valves.EZPRESTO_MCP_URL,
                json=payload,
                headers=call_headers,
                timeout=self.valves.TIMEOUT_SECONDS,
                verify=self.valves.VERIFY_TLS,
            )

        r = _do_call(self._session_id)

        # If session expired/invalid, re-init once and retry
        if r.status_code in (400, 404) and (
            "session" in r.text.lower() or "invalid" in r.text.lower()
        ):
            self._session_id = None
            self._mcp_new_session(headers_base)
            r = _do_call(self._session_id)

        if r.status_code >= 400:
            raise RuntimeError(
                f"MCP call failed status={r.status_code} "
                f"tool={tool_name} body={r.text[:400]!r} "
                f"session={self._session_id[:20] if self._session_id else 'NONE'}..."
            )

        # Support SSE-formatted responses (data: {...}\n\n)
        text = r.text.strip()
        parsed = None
        if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    parsed = json.loads(line[5:].strip())
                    break
        else:
            parsed = r.json()

        if parsed is None:
            raise RuntimeError(f"MCP call: could not parse response: {r.text[:400]!r}")

        if "error" in parsed:
            raise RuntimeError(f"MCP error: {parsed['error']}")

        result = parsed.get("result", {}) or {}
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                raw = item.get("text", "")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            if item.get("type") == "json":
                return item.get("json")
        return result

    def _mlis_headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.valves.MLIS_API_KEY:
            h["Authorization"] = f"Bearer {self.valves.MLIS_API_KEY}"
        return h

    def _mlis_predict(self, feature_rows: list[dict]) -> list[float]:
        r = requests.post(
            f"{self.valves.MLIS_ENDPOINT_URL.rstrip('/')}/predict",
            json={"instances": feature_rows},
            headers=self._mlis_headers(),
            timeout=self.valves.TIMEOUT_SECONDS,
            verify=self.valves.VERIFY_TLS,
        )
        r.raise_for_status()
        return r.json()["predictions"]

    def _mlis_explain(self, feature_row: dict) -> dict:
        r = requests.post(
            f"{self.valves.MLIS_ENDPOINT_URL.rstrip('/')}/explain",
            json={"row": feature_row},   # BentoML v1.4 wraps single-object args under the param name
            headers=self._mlis_headers(),
            timeout=self.valves.TIMEOUT_SECONDS,
            verify=self.valves.VERIFY_TLS,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _rows_from_ezpresto(payload: Any) -> list[dict]:
        """Normalise EzPresto MCP result to a list of dict rows.

        Different MCP-Presto servers return different shapes:
          - {"columns": [...], "rows": [[...], ...]}
          - {"data": [{...}, {...}]}
          - Just a list of dicts
        """
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            return []
        if "data" in payload and isinstance(payload["data"], list):
            return [r for r in payload["data"] if isinstance(r, dict)]
        cols = payload.get("columns") or payload.get("column_names")
        rows = payload.get("rows") or payload.get("data")
        if cols and rows:
            return [dict(zip(cols, row)) for row in rows]
        return []

    def _select_columns(self, row: dict, cols: list[str]) -> dict:
        return {c: row.get(c) for c in cols if c in row}

    def _err(self, msg: str, detail: Optional[str] = None) -> str:
        d = {"error": msg}
        if detail:
            d["detail"] = detail[:500]
        return json.dumps(d)

    # ---- tools exposed to the LLM ----------------------------------------

    def query_pipes(self, district: str, limit: int = 20) -> str:
        """
        Browse sewer pipes in a district WITHOUT ranking or scoring.

        Use this to answer "how many pipes in X", "what materials are in Y", or to
        surface a small sample. For prioritised inspection lists use rank_pipes.

        :param district: District code — one of NORTH, SOUTH, EAST, WEST.
        :param limit: Max rows to return (1-500). Default 20.
        :return: JSON string with {district, count, pipes: [...]}.
        """
        try:
            limit = max(1, min(int(limit), 500))
            sql = (
                f"SELECT pipe_id, district_code, material, year_installed, "
                f"age_years, diameter_mm, depth_m, nearest_tree_dist_m, "
                f"dominant_species, trees_within_15m, historical_incident_count, "
                f"soil_class, traffic_load_class "
                f"FROM {PIPES_TABLE} "
                f"WHERE district_code = '{district.upper()}' "
                f"LIMIT {limit}"
            )
            payload = self._mcp_call(
                self.valves.EZPRESTO_SQL_TOOL, {"query": sql}
            )
            rows = self._rows_from_ezpresto(payload)
            if not rows:
                return json.dumps({"district": district.upper(), "count": 0, "pipes": []})
            return json.dumps({
                "district": district.upper(),
                "count": len(rows),
                "pipes": rows,
            }, default=str)
        except Exception as e:
            return self._err("query_pipes failed", str(e))

    def rank_pipes(
        self,
        district: str,
        top_k: int = 50,
        budget: Optional[int] = None,
    ) -> str:
        """
        Rank sewer pipes in a district by predicted tree root infiltration risk.

        Internally: fetches candidate rows from EzPresto, scores them via MLIS,
        sorts descending, returns top_k. Use this for CCTV inspection planning.

        :param district: District code — one of NORTH, SOUTH, EAST, WEST.
        :param top_k: Number of top-risk pipes to return (1-500). Default 50.
        :param budget: Optional inspection budget for narrative context; returned unchanged.
        :return: JSON string with {district, top_k, budget, returned, pipes: [...]} sorted by risk_score.
        """
        try:
            top_k = max(1, min(int(top_k), 500))
            # Union display + feature columns so MLIS has everything it needs to score
            # (features) AND the LLM has context to present (display attributes).
            all_cols = list(dict.fromkeys(DISPLAY_COLS + FEATURE_COLS))
            select_expr = ", ".join(all_cols)
            sql = (
                f"SELECT {select_expr} "
                f"FROM {PIPES_TABLE} "
                f"WHERE district_code = '{district.upper()}'"
            )
            payload = self._mcp_call(
                self.valves.EZPRESTO_SQL_TOOL, {"query": sql}
            )
            rows = self._rows_from_ezpresto(payload)
            if not rows:
                return json.dumps({
                    "district": district.upper(), "top_k": top_k,
                    "budget": budget, "returned": 0, "pipes": [],
                })

            # Extract feature vectors for MLIS. Cast int-typed features defensively
            # in case any driver returns them as float (BentoML pydantic is strict).
            int_features = {
                "material_ordinal",
                "trees_within_15m",
                "historical_incident_count",
            }
            feature_rows = []
            for row in rows:
                fr = {}
                for c in FEATURE_COLS:
                    v = row.get(c)
                    if v is None:
                        # Skip this row rather than send a null MLIS will reject
                        fr = None
                        break
                    fr[c] = int(v) if c in int_features else float(v)
                if fr is not None:
                    feature_rows.append(fr)

            if not feature_rows:
                return self._err(
                    "no scorable rows: every candidate had a null feature value"
                )

            scores = self._mlis_predict(feature_rows)

            # Pair scores back to rows (feature_rows may be shorter than rows if
            # we skipped nulls — track alignment via a parallel index)
            scored_rows = []
            score_idx = 0
            for row in rows:
                if any(row.get(c) is None for c in FEATURE_COLS):
                    continue
                row["risk_score"] = round(float(scores[score_idx]), 4)
                scored_rows.append(row)
                score_idx += 1

            scored_rows.sort(key=lambda r: r["risk_score"], reverse=True)
            top = scored_rows[:top_k]
            for i, row in enumerate(top, 1):
                row["rank"] = i

            # Return display cols + rank + score (drop raw feature cols to keep
            # response compact and readable in chat)
            response_pipes = [
                {**{c: r.get(c) for c in DISPLAY_COLS},
                 "rank": r["rank"], "risk_score": r["risk_score"]}
                for r in top
            ]

            return json.dumps({
                "district": district.upper(),
                "top_k": top_k,
                "budget": budget,
                "returned": len(response_pipes),
                "pipes": response_pipes,
            }, default=str)
        except Exception as e:
            return self._err("rank_pipes failed", str(e))

    def explain_pipe(self, pipe_id: str) -> str:
        """
        Explain why a specific pipe scores high or low for tree root risk.

        Returns the top-5 feature contributions (SHAP-style) — which factors push
        risk up, which push it down, and by how much. Use to justify prioritising
        a specific pipe or to answer "why is this pipe risky".

        :param pipe_id: Pipe identifier from a prior query_pipes or rank_pipes call, e.g. "P-000123".
        :return: JSON with {pipe_id, risk_score, bias, top_contributions, attributes}.
        """
        try:
            feature_expr = ", ".join(FEATURE_COLS)
            display_expr = ", ".join(DISPLAY_COLS)
            # Combine display + features (they overlap; dedupe)
            all_cols = list(dict.fromkeys(DISPLAY_COLS + FEATURE_COLS))
            select_expr = ", ".join(all_cols)
            # single-quote escape for pipe_id defensive input
            safe_id = str(pipe_id).replace("'", "''")
            sql = (
                f"SELECT {select_expr} "
                f"FROM {PIPES_TABLE} "
                f"WHERE pipe_id = '{safe_id}' "
                f"LIMIT 1"
            )
            payload = self._mcp_call(
                self.valves.EZPRESTO_SQL_TOOL, {"query": sql}
            )
            rows = self._rows_from_ezpresto(payload)
            if not rows:
                return self._err(f"pipe '{pipe_id}' not found")
            row = rows[0]

            # Cast int-typed features defensively (BentoML pydantic is strict about
            # int vs float even when the value is whole)
            int_features = {
                "material_ordinal",
                "trees_within_15m",
                "historical_incident_count",
            }
            feature_row = {}
            for c in FEATURE_COLS:
                v = row.get(c)
                if v is None:
                    return self._err(f"pipe '{pipe_id}' has null value for feature '{c}'")
                feature_row[c] = int(v) if c in int_features else float(v)

            explanation = self._mlis_explain(feature_row)

            attributes = self._select_columns(row, DISPLAY_COLS)
            return json.dumps({
                "pipe_id": pipe_id,
                "risk_score": explanation.get("risk_score"),
                "bias": explanation.get("bias"),
                "top_contributions": explanation.get("top_contributions", []),
                "attributes": attributes,
            }, default=str)
        except Exception as e:
            return self._err("explain_pipe failed", str(e))

    def ezpresto_sql(self, query: str) -> str:
        """
        Run an ad-hoc SQL query against EzPresto for questions the specialised
        tools cannot answer — cross-catalog JOINs, aggregations, GROUP BY,
        arbitrary filters. Do NOT use this for questions the other tools already
        cover: prefer rank_pipes for ranking, explain_pipe for per-pipe
        explanations, query_pipes for simple district browsing.

        Available tables:

          waterutility.default.pipes — one row per sewer pipe
            columns:
              pipe_id (varchar), district_code (varchar), material (varchar),
              lat (double), lon (double), year_installed (bigint),
              dominant_species (varchar), soil_class (varchar),
              traffic_load_class (varchar), age_years (double),
              material_ordinal (bigint), diameter_mm (double), slope_pct (double),
              depth_m (double), nearest_tree_dist_m (double),
              trees_within_15m (bigint), dominant_species_riskscore (double),
              historical_incident_count (bigint), has_root_incident_5y (bigint)

          waterutility.default.work_orders — inspection/repair history
            columns:
              work_order_id (varchar), pipe_id (varchar),
              opened_date (varchar), closed_date (varchar),
              work_type (varchar), crew (varchar), cost_aud (bigint)

        Common patterns:
          -- pipes never inspected (LEFT JOIN, check right-side work_order_id)
          SELECT COUNT(*) FROM waterutility.default.pipes p
            LEFT JOIN waterutility.default.work_orders w
              ON p.pipe_id = w.pipe_id
            WHERE w.work_order_id IS NULL
          -- spend by crew
          SELECT crew, SUM(cost_aud) AS total_aud
            FROM waterutility.default.work_orders GROUP BY crew

        SQL syntax notes:
          - Do NOT end queries with a semicolon.
          - Use catalog.schema.table format for all table references.
          - When LEFT JOIN-ing then filtering nulls, check a non-key column
            like work_order_id (USING clause hides the join key).

        :param query: A single Presto/Trino SQL statement. Read-only queries only (SELECT).
        :return: JSON with {columns, rows} or {error} on failure.
        """
        try:
            q = str(query).strip().rstrip(";")
            if not q.lower().startswith(("select", "with", "show", "describe")):
                return self._err(
                    "only read-only queries allowed (SELECT / WITH / SHOW / DESCRIBE)"
                )
            payload = self._mcp_call(
                self.valves.EZPRESTO_SQL_TOOL, {"query": q}
            )
            rows = self._rows_from_ezpresto(payload)
            if not rows:
                return json.dumps({"columns": [], "rows": [], "count": 0})
            columns = list(rows[0].keys())
            return json.dumps({
                "columns": columns,
                "rows": rows,
                "count": len(rows),
            }, default=str)
        except Exception as e:
            return self._err("ezpresto_sql failed", str(e))
