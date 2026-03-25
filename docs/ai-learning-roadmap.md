# AI助手"变聪明"路线图

> Created: 2026-03-25 | Coco审查: revise(0.93) 9条建议全部采纳

## 核心原则

1. **实时数据是唯一真源** — 知识库只提供解释/术语/规则，不替代MCP实时查询
2. **问题路由层** — 静态知识走FTS5检索，动态KPI数据走MCP/数据库
3. **知识库需治理** — 每条知识有审核状态、置信度、来源追溯
4. **结构化注入** — 检索结果带source_id/score/scope，不拼接进system prompt

## 阶段1a：Schema + 问题路由 + 检索接入（1周）

### 改动文件
1. `db/sqlite_db.py` — 新增knowledge_base表 + FTS5虚拟表 + retrieval_feedback表
2. 新建`services/knowledge_service.py` — FTS5搜索 + 问题路由（静态vs动态）+ 结构化结果
3. `services/chat_service.py` — _build_full_prompt注入检索结果（结构化，非拼接）

### knowledge_base表设计
```sql
CREATE TABLE knowledge_base (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  category TEXT NOT NULL,        -- 'glossary'|'rule'|'faq'|'process'
  tags TEXT,                     -- 逗号分隔：bonus,kpi,settlement
  source_message_id TEXT,        -- 来源消息ID（可追溯）
  confidence REAL DEFAULT 0.5,   -- 0-1，人工审核后提升
  status TEXT DEFAULT 'draft',   -- 'draft'|'verified'|'archived'
  scope TEXT,                    -- 适用范围：'all'|'store_manager'|'admin'
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  verified_by TEXT,              -- 审核人
  verified_at TEXT
);

CREATE VIRTUAL TABLE kb_fts USING fts5(question, answer, tags, content=knowledge_base, content_rowid=id);

CREATE TABLE retrieval_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  retrieved_kb_ids TEXT,          -- JSON array of matched KB IDs
  route_decision TEXT,            -- 'static'|'dynamic'|'hybrid'
  user_feedback TEXT,             -- 'correct'|'incorrect'|'partial'|'resolved'|'unresolved'
  prompt_version TEXT,
  mcp_tools_used TEXT,            -- JSON array
  created_at TEXT NOT NULL
);
```

### 问题路由逻辑
```
用户提问
  ↓
路由层判断：
  - 含具体数字/门店/月份/年份 → 动态（MCP查询）
  - 含"什么是"/"如何计算"/"规则"/"流程" → 静态（FTS5检索）
  - 混合 → 先FTS5补充知识，再MCP查数据
  ↓
注入格式：
  ## Retrieved Knowledge (for reference only — do NOT use cached numbers)
  [1] (score:0.85, category:rule) Q: ... A: ...
  [2] (score:0.72, category:glossary) Q: ... A: ...
```

## 阶段1b：知识提取 + 反馈闭环（1周）

### 改动文件
4. 新建`scripts/extract_knowledge.py` — 从thumbs-up消息批量提取Q&A
5. `routers/chat.py` — feedback端点增强（正确性 + 解决率）
6. 新建`scripts/build_eval_set.py` — 生成离线评测集（100题）

### 知识提取规则
- 只从thumbs-up消息中提取
- 提取后status='draft'，需人工审核→'verified'
- 去除含具体数字的答案（避免过期数据固化）
- 保留来源message_id（可追溯）

### 反馈分类
- `correct` — 答案正确且有用
- `incorrect` — 答案有误
- `partial` — 部分有用
- `resolved` — 解决了我的问题
- `unresolved` — 没解决

## 阶段2：自适应 + 可观测（2-3周）

- Prompt版本化存DB（可回滚）
- 解释性答案缓存（仅status='verified'的静态知识，不缓存数字）
- 月度报告脚本：top10失败问题、检索命中率、反馈分布
- 离线评测自动跑（CI集成）

## 阶段3：候选方向（不排期）

> Coco建议：需先完成RBAC、记忆TTL、可删除性和审计日志，再考虑

- 用户画像（常见问题模式）
- 跨对话记忆
- 工具效果矩阵（先用日志分析替代）

## 技术选型

| 决策 | 选择 | 理由 |
|------|------|------|
| 搜索 | SQLite FTS5 | 已有SQLite+WAL，零依赖，150人规模够用 |
| 向量DB | 不用 | 400-600条知识，关键词足够 |
| 知识提取 | Claude CLI批处理 | 一次性，不需要额外服务 |
| 缓存 | 仅解释性答案 | KPI数据时效性太强 |
