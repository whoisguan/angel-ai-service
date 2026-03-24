"""MCP Server for KPI data access.

Provides tools that Claude can call to query KPI/BI data.
Launched by claude CLI via stdio transport.
Receives user permissions via environment variables.

Usage:
    claude -p "query" --mcp-config '{"mcpServers":{"kpi-data":{"command":"python","args":["mcp_server.py"],"env":{"KPI_DATABASE_URL":"...","USER_STORE_IDS":"1,2,3"}}}}'
"""

import json
import os
import sys

# MCP protocol over stdio — minimal implementation
# Uses the MCP SDK if available, falls back to raw JSON-RPC


def get_user_store_ids() -> list[int] | None:
    """Get permitted store IDs from environment.

    Returns:
        None: admin (USER_STORE_IDS="*"), can see all stores
        []: no permission (USER_STORE_IDS empty or not set)
        [1,2,3]: specific store access
    """
    raw = os.environ.get("USER_STORE_IDS", "")
    if not raw:
        return []  # empty = NO permission (safe default)
    if raw.strip() == "*":
        return None  # explicit admin marker
    ids = []
    for s in raw.split(","):
        s = s.strip()
        if s.isdigit():
            ids.append(int(s))
    return ids if ids else []


def get_db_connection():
    """Get database connection from environment."""
    import pymysql
    url = os.environ.get("KPI_DATABASE_URL", "")
    # Parse mysql+pymysql://user:pass@host/db
    if not url:
        return None
    # Simple URL parsing
    from urllib.parse import urlparse
    parsed = urlparse(url.replace("mysql+pymysql://", "mysql://"))
    return pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


# --- Tool implementations ---

def tool_get_store_performance(args: dict) -> dict:
    """Query store performance: revenue, growth, KPI score, bonus pool."""
    store_id = args.get("store_id")
    year = args["year"]
    month = args.get("month")
    user_stores = get_user_store_ids()

    # Permission check
    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}
    if store_id and user_stores is not None and store_id not in user_stores:
        return {"error": "You don't have permission to view this store."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        with conn.cursor() as cur:
            base_query = """
                SELECT s.store_code, s.name as store_name,
                       ms.year, ms.month,
                       ms.total_revenue, ms.baseline_revenue,
                       ms.surplus_amount, ms.growth_rate,
                       ms.final_score, ms.final_bonus_pool,
                       ms.is_frozen
                FROM monthly_settlement ms
                JOIN stores s ON ms.store_id = s.id
                WHERE ms.year = %s
            """
            params = [year]

            if month:
                base_query += " AND ms.month = %s"
                params.append(month)

            if store_id:
                base_query += " AND ms.store_id = %s"
                params.append(store_id)
            elif user_stores is not None:  # None=admin(no filter), []=blocked(caught above)
                placeholders = ",".join(["%s"] * len(user_stores))
                base_query += f" AND ms.store_id IN ({placeholders})"
                params.extend(user_stores)

            base_query += " ORDER BY ms.month, s.store_code"
            cur.execute(base_query, params)
            rows = cur.fetchall()

            # Convert Decimal to float for JSON serialization
            for row in rows:
                for key, val in row.items():
                    if hasattr(val, "as_integer_ratio"):  # Decimal/float check
                        row[key] = float(val)

            return {"stores": rows, "count": len(rows)}
    finally:
        conn.close()


def tool_get_employee_bonus(args: dict) -> dict:
    """Query employee bonus details."""
    employee_id = args.get("employee_id")
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        with conn.cursor() as cur:
            query = """
                SELECT e.first_name, e.last_name, s.store_code, s.name as store_name,
                       d.dept_code, pb.bonus_gross, pb.kpi_score, pb.kpi_factor,
                       pb.bonus_net, pb.is_consolation
                FROM personal_bonus pb
                JOIN employees e ON pb.employee_id = e.id
                JOIN stores s ON pb.store_id = s.id
                JOIN departments d ON pb.department_id = d.id
                WHERE pb.year = %s AND pb.month = %s
            """
            params = [year, month]

            if employee_id:
                query += " AND pb.employee_id = %s"
                params.append(employee_id)

            if user_stores:
                placeholders = ",".join(["%s"] * len(user_stores))
                query += f" AND pb.store_id IN ({placeholders})"
                params.extend(user_stores)

            query += " ORDER BY e.last_name, e.first_name"
            cur.execute(query, params)
            rows = cur.fetchall()

            for row in rows:
                for key, val in row.items():
                    if hasattr(val, "as_integer_ratio"):
                        row[key] = float(val)

            return {"bonuses": rows, "count": len(rows)}
    finally:
        conn.close()


def _serialize_rows(rows: list[dict]):
    """Convert Decimal/numeric values to float for JSON serialization."""
    for row in rows:
        for key, val in row.items():
            if hasattr(val, "as_integer_ratio"):
                row[key] = float(val)


def tool_get_store_ranking(args: dict) -> dict:
    """Get store rankings by KPI score for a given month."""
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view store rankings."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        with conn.cursor() as cur:
            query = """
                SELECT s.store_code, s.name as store_name,
                       ms.final_score, ms.growth_rate, ms.total_revenue,
                       ms.final_bonus_pool,
                       RANK() OVER (ORDER BY ms.final_score DESC) as rank_score
                FROM monthly_settlement ms
                JOIN stores s ON ms.store_id = s.id
                WHERE ms.year = %s AND ms.month = %s
            """
            params = [year, month]

            if user_stores is not None:
                placeholders = ",".join(["%s"] * len(user_stores))
                query += f" AND ms.store_id IN ({placeholders})"
                params.extend(user_stores)

            query += " ORDER BY ms.final_score DESC"
            cur.execute(query, params)
            rows = cur.fetchall()
            _serialize_rows(rows)
            return {"rankings": rows, "count": len(rows)}
    finally:
        conn.close()


# --- MCP Protocol Handler (stdio JSON-RPC) ---

TOOLS = {
    "get_store_performance": {
        "description": "Query store KPI performance: revenue, growth rate, final score, bonus pool. Filter by store and time period.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Year (e.g. 2026)"},
                "month": {"type": "integer", "description": "Month 1-12 (optional, omit for full year)"},
                "store_id": {"type": "integer", "description": "Store ID (optional, omit for all accessible stores)"},
            },
            "required": ["year"],
        },
        "handler": tool_get_store_performance,
    },
    "get_employee_bonus": {
        "description": "Query employee bonus details: gross amount, KPI score, KPI factor, net amount. Respects user's store access scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "integer", "description": "Employee ID (optional)"},
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
            },
            "required": ["year", "month"],
        },
        "handler": tool_get_employee_bonus,
    },
    "get_store_ranking": {
        "description": "Get all stores ranked by KPI score for a specific month. Shows score, growth rate, revenue, and bonus pool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
            },
            "required": ["year", "month"],
        },
        "handler": tool_get_store_ranking,
    },
}


def handle_request(request: dict) -> dict:
    """Handle a JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kpi-data", "version": "0.1.0"},
            },
        }

    elif method == "notifications/initialized":
        return None  # no response for notifications

    elif method == "tools/list":
        tool_list = []
        for name, tool in TOOLS.items():
            tool_list.append({
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            })
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tool_list},
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            result = TOOLS[tool_name]["handler"](tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def main():
    """Main loop: read JSON-RPC from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
