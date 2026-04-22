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
from contextvars import ContextVar

# MCP protocol over stdio — minimal implementation
# Uses the MCP SDK if available, falls back to raw JSON-RPC

_NO_OVERRIDE = object()
_CTX_USER_STORE_IDS: ContextVar[list[int] | None | object] = ContextVar("CTX_USER_STORE_IDS", default=_NO_OVERRIDE)
_CTX_KPI_DATABASE_URL: ContextVar[str | object] = ContextVar("CTX_KPI_DATABASE_URL", default=_NO_OVERRIDE)


def set_runtime_context(user_store_ids: list[int] | None, kpi_database_url: str) -> tuple[object, object]:
    """Set per-request context (thread-safe via contextvars).

    This allows the in-process LLM backend (e.g. Gemini) to call the same tool
    implementations without relying on global environment variables.

    Returns context tokens that must be passed to reset_runtime_context().
    """
    t1 = _CTX_USER_STORE_IDS.set(user_store_ids)
    t2 = _CTX_KPI_DATABASE_URL.set(kpi_database_url)
    return t1, t2


def reset_runtime_context(tokens: tuple[object, object]) -> None:
    """Reset per-request context tokens returned by set_runtime_context()."""
    t1, t2 = tokens
    _CTX_USER_STORE_IDS.reset(t1)
    _CTX_KPI_DATABASE_URL.reset(t2)


def get_user_store_ids() -> list[int] | None:
    """Get permitted store IDs from environment.

    Returns:
        None: admin (USER_STORE_IDS="*"), can see all stores
        []: no permission (USER_STORE_IDS empty or not set)
        [1,2,3]: specific store access
    """
    override = _CTX_USER_STORE_IDS.get()
    if override is not _NO_OVERRIDE:
        return override  # None = admin, [] = blocked, [ids] = scoped

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
    """Get database connection via pyodbc (SQL Server)."""
    import pyodbc
    override = _CTX_KPI_DATABASE_URL.get()
    conn_str = override if override is not _NO_OVERRIDE else os.environ.get("KPI_DATABASE_URL", "")
    if not conn_str:
        return None
    conn = pyodbc.connect(conn_str)
    return conn


def _fetchall_as_dicts(cursor) -> list[dict]:
    """Convert pyodbc cursor rows to list of dicts (replaces DictCursor)."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


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
        cur = conn.cursor()
        base_query = """
            SELECT s.store_code, s.name as store_name,
                   ms.year, ms.month,
                   ms.total_revenue, ms.baseline_revenue,
                   ms.surplus_amount, ms.surplus_growth_rate,
                   ms.final_score, ms.final_bonus_pool,
                   ms.is_frozen
            FROM monthly_settlement ms
            JOIN stores s ON ms.store_id = s.id
            WHERE ms.year = ?
        """
        params = [year]

        if month:
            base_query += " AND ms.month = ?"
            params.append(month)

        if store_id:
            base_query += " AND ms.store_id = ?"
            params.append(store_id)
        elif user_stores is not None:  # None=admin(no filter), []=blocked(caught above)
            placeholders = ",".join(["?"] * len(user_stores))
            base_query += f" AND ms.store_id IN ({placeholders})"
            params.extend(user_stores)

        base_query += " ORDER BY ms.month, s.store_code"
        cur.execute(base_query, params)
        rows = _fetchall_as_dicts(cur)

        # Convert Decimal to float for JSON serialization
        for row in rows:
            for key, val in row.items():
                if hasattr(val, "as_integer_ratio"):  # Decimal/float check
                    row[key] = float(val)

        return {"stores": rows, "count": len(rows)}
    finally:
        cur.close()
        conn.close()


def tool_get_employee_bonus(args: dict) -> dict:
    """Query employee bonus details."""
    employee_id = args.get("employee_id")
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    # H2 fix: empty list = no permission, not admin bypass
    if user_stores is not None and not user_stores:
        return {"error": "No store access permission."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        query = """
            SELECT e.first_name, e.last_name, s.store_code, s.name as store_name,
                   d.dept_code, pb.bonus_gross, pb.kpi_score, pb.kpi_factor,
                   pb.bonus_net
            FROM personal_bonus pb
            JOIN employees e ON pb.employee_id = e.id
            JOIN stores s ON pb.store_id = s.id
            JOIN departments d ON pb.dept_id = d.id
            WHERE pb.year = ? AND pb.month = ?
        """
        params = [year, month]

        if employee_id:
            query += " AND pb.employee_id = ?"
            params.append(employee_id)

        if user_stores is not None:
            placeholders = ",".join(["?"] * len(user_stores))
            query += f" AND pb.store_id IN ({placeholders})"
            params.extend(user_stores)

        query += " ORDER BY e.last_name, e.first_name"
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)

        for row in rows:
            for key, val in row.items():
                if hasattr(val, "as_integer_ratio"):
                    row[key] = float(val)

        return {"bonuses": rows, "count": len(rows)}
    finally:
        cur.close()
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
        cur = conn.cursor()
        query = """
            SELECT s.store_code, s.name as store_name,
                   ms.final_score, ms.surplus_growth_rate, ms.total_revenue,
                   ms.final_bonus_pool,
                   RANK() OVER (ORDER BY ms.final_score DESC) as rank_score
            FROM monthly_settlement ms
            JOIN stores s ON ms.store_id = s.id
            WHERE ms.year = ? AND ms.month = ?
        """
        params = [year, month]

        if user_stores is not None:
            placeholders = ",".join(["?"] * len(user_stores))
            query += f" AND ms.store_id IN ({placeholders})"
            params.extend(user_stores)

        query += " ORDER BY ms.final_score DESC"
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)
        return {"rankings": rows, "count": len(rows)}
    finally:
        cur.close()
        conn.close()


def tool_get_employee_score(args: dict) -> dict:
    """Query employee KPI score details: layer1/layer2/layer3 and total score."""
    employee_id = args.get("employee_id")
    store_id = args.get("store_id")
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}
    if store_id and user_stores is not None and store_id not in user_stores:
        return {"error": "You don't have permission to view this store."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        query = """
            SELECT e.first_name, e.last_name, s.store_code, s.name as store_name,
                   sr.total_score, sr.weighted_score,
                   sr.year, sr.month
            FROM score_records sr
            JOIN employees e ON sr.target_employee_id = e.id
            JOIN stores s ON sr.target_store_id = s.id
            WHERE sr.year = ? AND sr.month = ?
        """
        params = [year, month]

        if employee_id:
            query += " AND sr.target_employee_id = ?"
            params.append(employee_id)

        if store_id:
            query += " AND sr.target_store_id = ?"
            params.append(store_id)
        elif user_stores is not None:
            placeholders = ",".join(["?"] * len(user_stores))
            query += f" AND sr.target_store_id IN ({placeholders})"
            params.extend(user_stores)

        query += " ORDER BY sr.total_score DESC, e.last_name, e.first_name"
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)
        return {"scores": rows, "count": len(rows)}
    finally:
        cur.close()
        conn.close()


def tool_get_score_trend(args: dict) -> dict:
    """Query KPI score trend over multiple months for a store or employee."""
    entity_type = args["entity_type"]  # STORE or EMPLOYEE
    entity_id = args["entity_id"]
    year = args["year"]
    month_from = args.get("month_from", 1)
    month_to = args.get("month_to", 12)
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        if entity_type == "STORE":
            # Permission check for store
            if user_stores is not None and entity_id not in user_stores:
                return {"error": "You don't have permission to view this store."}

            query = """
                SELECT ms.month, ms.final_score as score,
                       s.store_code, s.name as store_name
                FROM monthly_settlement ms
                JOIN stores s ON ms.store_id = s.id
                WHERE ms.store_id = ? AND ms.year = ?
                  AND ms.month >= ? AND ms.month <= ?
                ORDER BY ms.month
            """
            params = [entity_id, year, month_from, month_to]

        elif entity_type == "EMPLOYEE":
            query = """
                SELECT sr.month, sr.total_score as score,
                       e.first_name, e.last_name,
                       s.store_code, s.name as store_name
                FROM score_records sr
                JOIN employees e ON sr.target_employee_id = e.id
                JOIN stores s ON sr.target_store_id = s.id
                WHERE sr.target_employee_id = ? AND sr.year = ?
                  AND sr.month >= ? AND sr.month <= ?
            """
            params = [entity_id, year, month_from, month_to]

            # Permission check: filter by accessible stores
            if user_stores is not None:
                placeholders = ",".join(["?"] * len(user_stores))
                query += f" AND sr.target_store_id IN ({placeholders})"
                params.extend(user_stores)

            query += " ORDER BY sr.month"

        else:
            return {"error": f"Invalid entity_type: {entity_type}. Use STORE or EMPLOYEE."}

        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)

        # Compute trend summary
        scores = [r["score"] for r in rows if r["score"] is not None]
        summary = {}
        if scores:
            summary = {
                "avg": round(sum(scores) / len(scores), 2),
                "best": max(scores),
                "worst": min(scores),
                "months_count": len(scores),
            }

        return {"trend": rows, "summary": summary}
    finally:
        cur.close()
        conn.close()


def tool_get_scoring_completion(args: dict) -> dict:
    """Query scoring completion status: total, completed, pending, rate."""
    year = args["year"]
    month = args["month"]
    store_id = args.get("store_id")
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}
    if store_id and user_stores is not None and store_id not in user_stores:
        return {"error": "You don't have permission to view this store."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        query = """
            SELECT s.store_code, s.name as store_name,
                   COUNT(*) as total_tasks,
                   SUM(CASE WHEN st.status = 'completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN st.status != 'completed' THEN 1 ELSE 0 END) as pending,
                   ROUND(SUM(CASE WHEN st.status = 'completed' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as completion_rate
            FROM score_tasks st
            JOIN stores s ON st.target_store_id = s.id
            WHERE st.year = ? AND st.month = ?
        """
        params = [year, month]

        if store_id:
            query += " AND st.target_store_id = ?"
            params.append(store_id)
        elif user_stores is not None:
            placeholders = ",".join(["?"] * len(user_stores))
            query += f" AND st.target_store_id IN ({placeholders})"
            params.extend(user_stores)

        query += " GROUP BY s.store_code, s.name ORDER BY s.store_code"
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)

        # Compute overall summary
        total = sum(r["total_tasks"] for r in rows)
        completed = sum(r["completed"] for r in rows)
        pending = sum(r["pending"] for r in rows)
        overall_rate = round(completed * 100.0 / total, 1) if total > 0 else 0.0

        return {
            "by_store": rows,
            "overall": {
                "total_tasks": total,
                "completed": completed,
                "pending": pending,
                "completion_rate": overall_rate,
            },
        }
    finally:
        cur.close()
        conn.close()


def tool_get_department_bonus(args: dict) -> dict:
    """Query department-level bonus allocation for a store."""
    store_id = args["store_id"]
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}
    if user_stores is not None and store_id not in user_stores:
        return {"error": "You don't have permission to view this store."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        query = """
            SELECT d.dept_code, d.dept_name_it as dept_name,
                   db.bonus_amount
            FROM dept_bonus db
            JOIN departments d ON db.dept_id = d.id
            WHERE db.store_id = ? AND db.year = ? AND db.month = ?
            ORDER BY db.bonus_amount DESC
        """
        params = [store_id, year, month]
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)
        return {"departments": rows, "count": len(rows), "store_id": store_id}
    finally:
        cur.close()
        conn.close()


def tool_get_config_params(args: dict) -> dict:
    """Query configuration parameters (public, no permission check)."""
    param_category = args.get("param_category")

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        cur = conn.cursor()
        query = """
            SELECT param_category, param_name, param_value, description
            FROM config_params
        """
        params = []

        if param_category:
            query += " WHERE param_category = ?"
            params.append(param_category)

        query += " ORDER BY param_category, param_name"
        cur.execute(query, params)
        rows = _fetchall_as_dicts(cur)
        _serialize_rows(rows)
        return {"params": rows, "count": len(rows)}
    finally:
        cur.close()
        conn.close()


# Pre-defined explanation texts for calculation rules (Chinese + Italian)
_CALCULATION_EXPLANATIONS = {
    "progressive_tiers": {
        "zh": (
            "累进制分档：根据门店营业额超出基线的比例，分段计算奖金池。"
            "例如：超出0-10%部分按A%提取，10-20%部分按B%提取，以此类推。"
            "档位越高，提取比例越大，激励门店持续突破。"
        ),
        "it": (
            "Scaglioni progressivi: il pool bonus viene calcolato in base alla percentuale "
            "di superamento del fatturato rispetto alla baseline, con aliquote crescenti "
            "per ogni scaglione."
        ),
    },
    "category_adjustment": {
        "zh": (
            "品类调整系数：根据门店主营品类的市场难度，对奖金池进行系数调整。"
            "不同品类的调整系数在config_params中配置。"
        ),
        "it": (
            "Coefficiente di aggiustamento per categoria: il pool bonus viene moltiplicato "
            "per un coefficiente basato sulla difficoltà di mercato della categoria principale."
        ),
    },
    "dept_allocation": {
        "zh": (
            "部门分配：门店总奖金池按部门分配比例（dept_share）拆分到各部门。"
            "分配比例由管理层设定，反映各部门对业绩的贡献权重。"
        ),
        "it": (
            "Allocazione dipartimentale: il pool bonus totale viene suddiviso tra i dipartimenti "
            "secondo le quote stabilite (dept_share)."
        ),
    },
    "personal_coefficient": {
        "zh": (
            "个人系数：由员工的岗位级别、工龄、特殊角色等因素决定。"
            "个人系数影响该员工在部门奖金中的分配份额。"
        ),
        "it": (
            "Coefficiente personale: determinato dal livello, anzianità e ruolo speciale del dipendente. "
            "Influenza la quota di bonus dipartimentale assegnata."
        ),
    },
    "kpi_factor": {
        "zh": (
            "KPI因子：根据员工KPI评分转换为0-1.2的乘数。"
            "评分越高，KPI因子越大，最终奖金 = 基础奖金 × KPI因子。"
            "低于及格线的员工KPI因子为0，不发放绩效奖金（但可能有保底）。"
        ),
        "it": (
            "Fattore KPI: il punteggio KPI viene convertito in un moltiplicatore (0-1.2). "
            "Bonus finale = bonus base × fattore KPI. Sotto la soglia minima, il fattore è 0."
        ),
    },
    "minimum_payout": {
        "zh": (
            "保底机制：即使员工KPI评分不达标，仍可获得一笔保底奖金（consolation bonus）。"
            "保底金额在config_params中配置，确保基本激励。"
        ),
        "it": (
            "Pagamento minimo: anche se il punteggio KPI è insufficiente, il dipendente riceve "
            "un bonus di consolazione configurato nei parametri."
        ),
    },
    "reserve_fund": {
        "zh": (
            "储备金：每月从门店奖金池中提取一定比例作为储备金。"
            "储备金用于年终调节、特殊奖励或弥补淡季。"
        ),
        "it": (
            "Fondo di riserva: una percentuale del pool bonus mensile viene accantonata. "
            "Utilizzata per conguagli di fine anno, premi speciali o compensazione dei mesi deboli."
        ),
    },
    "scoring_v3_layers": {
        "zh": (
            "V3三层评分体系：\n"
            "Layer 1 — 业绩指标（营业额、增长率等客观数据）\n"
            "Layer 2 — 管理指标（库存管理、客户服务、团队协作等）\n"
            "Layer 3 — 额外加减分（特殊贡献、违规扣分等）\n"
            "总分 = Layer1 + Layer2 + Layer3，满分100分。"
        ),
        "it": (
            "Sistema di valutazione V3 a tre livelli:\n"
            "Layer 1 — Indicatori di performance (fatturato, crescita)\n"
            "Layer 2 — Indicatori gestionali (inventario, servizio clienti, teamwork)\n"
            "Layer 3 — Bonus/malus aggiuntivi (contributi speciali, penalità)\n"
            "Punteggio totale = Layer1 + Layer2 + Layer3, massimo 100."
        ),
    },
}


def tool_explain_calculation(args: dict) -> dict:
    """Return pre-defined explanation text for a calculation rule topic."""
    topic = args["topic"]
    explanation = _CALCULATION_EXPLANATIONS.get(topic)
    if not explanation:
        valid_topics = list(_CALCULATION_EXPLANATIONS.keys())
        return {"error": f"Unknown topic: {topic}. Valid topics: {valid_topics}"}
    return {"topic": topic, "explanation_zh": explanation["zh"], "explanation_it": explanation["it"]}


def tool_detect_anomalies(args: dict) -> dict:
    """Detect data anomalies in monthly settlement: score swings, zero surplus, low scores."""
    year = args["year"]
    month = args["month"]
    user_stores = get_user_store_ids()

    if user_stores is not None and not user_stores:
        return {"error": "You don't have permission to view any store data."}

    conn = get_db_connection()
    if not conn:
        return {"error": "Database connection not available."}

    try:
        anomalies = []
        cur = conn.cursor()
        # Build store filter clause
        store_filter = ""
        store_params = []
        if user_stores is not None:
            placeholders = ",".join(["?"] * len(user_stores))
            store_filter = f" AND ms.store_id IN ({placeholders})"
            store_params = list(user_stores)

        # 1. Detect score_swing: compare with previous month
        query_swing = f"""
            SELECT s.store_code, s.name as store_name,
                   ms.final_score as current_score,
                   prev.final_score as prev_score,
                   (ms.final_score - prev.final_score) as score_diff
            FROM monthly_settlement ms
            JOIN monthly_settlement prev
              ON ms.store_id = prev.store_id
              AND prev.year = CASE WHEN ? = 1 THEN ? - 1 ELSE ? END
              AND prev.month = CASE WHEN ? = 1 THEN 12 ELSE ? - 1 END
            JOIN stores s ON ms.store_id = s.id
            WHERE ms.year = ? AND ms.month = ?
              AND ABS(ms.final_score - prev.final_score) > 20
              {store_filter}
        """
        params_swing = [month, year, year, month, month, year, month] + store_params
        cur.execute(query_swing, params_swing)
        for row in _fetchall_as_dicts(cur):
            _serialize_rows([row])
            anomalies.append({
                "type": "score_swing",
                "severity": "high" if abs(row["score_diff"]) > 30 else "medium",
                "store_code": row["store_code"],
                "detail": (
                    f"{row['store_name']}: score changed by {row['score_diff']:+.1f} "
                    f"({row['prev_score']:.1f} -> {row['current_score']:.1f})"
                ),
            })

        # 2. Detect zero_surplus: stores with zero or negative surplus
        query_zero = f"""
            SELECT s.store_code, s.name as store_name,
                   ms.surplus_amount, ms.total_revenue, ms.baseline_revenue
            FROM monthly_settlement ms
            JOIN stores s ON ms.store_id = s.id
            WHERE ms.year = ? AND ms.month = ?
              AND ms.surplus_amount <= 0
              {store_filter}
        """
        params_zero = [year, month] + store_params
        cur.execute(query_zero, params_zero)
        for row in _fetchall_as_dicts(cur):
            _serialize_rows([row])
            anomalies.append({
                "type": "zero_surplus",
                "severity": "medium",
                "store_code": row["store_code"],
                "detail": (
                    f"{row['store_name']}: surplus={row['surplus_amount']:.2f} "
                    f"(revenue={row['total_revenue']:.2f}, baseline={row['baseline_revenue']:.2f})"
                ),
            })

        # 3. Detect low_score: stores with final_score below 50
        query_low = f"""
            SELECT s.store_code, s.name as store_name,
                   ms.final_score
            FROM monthly_settlement ms
            JOIN stores s ON ms.store_id = s.id
            WHERE ms.year = ? AND ms.month = ?
              AND ms.final_score < 50
              {store_filter}
        """
        params_low = [year, month] + store_params
        cur.execute(query_low, params_low)
        for row in _fetchall_as_dicts(cur):
            _serialize_rows([row])
            anomalies.append({
                "type": "low_score",
                "severity": "high" if row["final_score"] < 30 else "medium",
                "store_code": row["store_code"],
                "detail": f"{row['store_name']}: final_score={row['final_score']:.1f} (below 50)",
            })

        return {"anomalies": anomalies, "count": len(anomalies), "year": year, "month": month}
    finally:
        cur.close()
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
    "get_employee_score": {
        "description": "Query employee KPI score details: layer1, layer2, layer3 breakdown and total score. Filter by employee or store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "integer", "description": "Employee ID (optional)"},
                "store_id": {"type": "integer", "description": "Store ID (optional)"},
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
            },
            "required": ["year", "month"],
        },
        "handler": tool_get_employee_score,
    },
    "get_score_trend": {
        "description": "Query KPI score trend over multiple months for a store or employee. Returns monthly scores and trend summary (avg, best, worst).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": ["STORE", "EMPLOYEE"], "description": "STORE or EMPLOYEE"},
                "entity_id": {"type": "integer", "description": "Store ID or Employee ID"},
                "year": {"type": "integer", "description": "Year"},
                "month_from": {"type": "integer", "description": "Start month (default 1)"},
                "month_to": {"type": "integer", "description": "End month (default 12)"},
            },
            "required": ["entity_type", "entity_id", "year"],
        },
        "handler": tool_get_score_trend,
    },
    "get_scoring_completion": {
        "description": "Query scoring completion status: how many score tasks are completed vs pending, with completion rate per store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
                "store_id": {"type": "integer", "description": "Store ID (optional)"},
            },
            "required": ["year", "month"],
        },
        "handler": tool_get_scoring_completion,
    },
    "get_department_bonus": {
        "description": "Query department-level bonus allocation for a specific store: bonus amount, share ratio, headcount, and average per person.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer", "description": "Store ID"},
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
            },
            "required": ["store_id", "year", "month"],
        },
        "handler": tool_get_department_bonus,
    },
    "get_config_params": {
        "description": "Query system configuration parameters (e.g. progressive_tiers, dept_share, bonus_coefficient). Public data, no permission restriction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "param_category": {"type": "string", "description": "Filter by category (optional, e.g. 'progressive_tiers', 'dept_share', 'bonus_coefficient')"},
            },
            "required": [],
        },
        "handler": tool_get_config_params,
    },
    "explain_calculation": {
        "description": "Get a human-readable explanation of a KPI/bonus calculation rule, in both Chinese and Italian.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["progressive_tiers", "category_adjustment", "dept_allocation", "personal_coefficient", "kpi_factor", "minimum_payout", "reserve_fund", "scoring_v3_layers"],
                    "description": "The calculation topic to explain",
                },
            },
            "required": ["topic"],
        },
        "handler": tool_explain_calculation,
    },
    "detect_anomalies": {
        "description": "Detect data anomalies in monthly settlement: large score swings, zero/negative surplus, abnormally low scores. Respects user's store access scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Year"},
                "month": {"type": "integer", "description": "Month 1-12"},
            },
            "required": ["year", "month"],
        },
        "handler": tool_detect_anomalies,
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
            # M10 fix: log full error server-side, return generic message to client
            import sys
            print(f"[MCP] Tool '{tool_name}' error: {e}", file=sys.stderr)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "An internal error occurred while processing your request."}],
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
