# 文献数据提取与人工审核系统 — 设计文档（v1.1：MicroalgaeMatch 字段）

## 1. 目标

用 AI 从论文文字中提取结构化记录，**每条记录必须能追溯到原文出处**，再由审核人在网页上逐条确认 / 修改 / 拒绝，最终导出只包含已确认记录、并带出处的 CSV / Excel。

审核人只用网页，不用 Claude。因此本版不做 MCP；网页直接调用普通后端接口（FastAPI）。

## 2. 总体流程

```
API key (.env) → [AI 分段提取] → 三层 proposed 记录（论文 / 条件 / 观测值）+ 原始回答 + 版本号
                                    ↓
                     [关联解析 + 自动检查器] → parent_id + flag
                                    ↓
             [简易网页] ← 审核人：看原文高亮 → 确认 / 修改 / 拒绝 / 待解决
                                    ↓
              三层都 approved 的观测值 → 拼成一行 → 导出 CSV/Excel（模板列顺序）
```

## 3. 技术选择

| 方面 | 选择 | 理由 |
|---|---|---|
| 语言 / schema | Python 3.11+，Pydantic | 字段定义集中、可校验 |
| 存储 | SQLite（标准库 `sqlite3`） | 单文件、零运维 |
| LLM | OpenAI 兼容接口（`openai` SDK） | `base_url` / `model` / `api_key` 写在 `.env`，换供应商只改配置 |
| 后端 | FastAPI | 网页调用的普通 HTTP 接口 |
| 前端 | 纯 HTML + JS（无框架） | 保持简单 |
| 导出 | CSV（UTF-8 BOM，Excel 可直接打开）、xlsx（openpyxl） | |

## 4. 模块划分

```
audit/
  config.py          读取 .env
  fields.py          字段定义（唯一的字段来源：提示词、解析、检查器、网页、导出都从这里读）+ 临时选项
  documents.py       文档输入：纯文本载入 + 分段；PDF 解析留接口（未实现）
  textmatch.py       原文定位：在原文中找到引文，返回字符偏移（用于高亮）
  llm.py             LLM 客户端（OpenAI 兼容）+ 测试 / 演示用的回放客户端
  extract.py         分段提取：组提示词 → 调 LLM → 存原始回答 → 解析三层 → 写 proposed 记录
  checks.py          单条记录的自动检查器
  db.py              SQLite 建表与连接
  review_service.py  审核服务层：关联解析、系统编号、整篇重算 flag、列表、详情、各审核动作
  export.py          导出
  api.py             FastAPI 路由（薄封装 review_service / export）
  cli.py             命令行：建库、导入、提取、演示数据、启动网页、导出
  static/            网页（index.html / app.js / style.css）
```

`review_service` 是独立的一层，不依赖 FastAPI。以后如需 MCP，只需在它外面再包一层。

## 5. 数据模型

### 5.1 三层结构（方案 B2：三层，但仍是同一张 records 表）

| 层级 `kind` | 含义 | 字段来源 |
|---|---|---|
| `source` | 论文 / 来源 | FIELDS.md 的 Source 组（出处类字段除外）+ 论文级判定字段 |
| `condition` | 一个生物在一个处理、一个阶段下的条件 | Identity、Cultivation、Electrical setup、Availability 组 |
| `observation` | 一条测量结果 | Shared measurement fields 组 + `source_locator` / `evidence_excerpt` / `extraction_method` |

记录内容仍是 `{"fields": {字段名: {"value", "quote"}}}`。字段定义见 [FIELDS.md](FIELDS.md)，代码里在 [audit/fields.py](../audit/fields.py)，每个字段带 `level / type / essential / missing / filled_by / choices`。

`filled_by` 决定谁来填：

| 值 | 含义 |
|---|---|
| `ai` | AI 提取，附原文引文 |
| `link` | AI 写的关联标签（`condition_key`、`condition_ref`、`experiment_ref`、`arm_ref`、`*_ref`），不导出 |
| `system_id` | 系统根据关联生成的编号（`study_id`、`condition_id`、`observation_id` …） |
| `reviewer` | 来自审核层（`review_status` ← `records.status`，`reviewer_id`、`reviewed_at`、`reviewer_notes` ← 审核日志） |
| `later` | 不来自论文（`accepted_taxon_name`、Availability 组），v1 留空 |
| `derived` | 系统推出（`is_electrical`，由 trait_group / metric_code 判断） |

### 5.2 关联与编号

- AI 给每个条件一个 `condition_key`，每条观测值用 `condition_ref` 指向它。
- `review_service.refresh_document` 把标签解析成 `records.parent_id`：观测值 → 条件，条件 → 论文。优先匹配同一次 AI 调用的记录，其次匹配全文档的记录；被拒绝的记录不作为关联目标。
- 编号在读取时生成：`study_id` = `YYYY_SURNAME`（取不到就用 `DOC<id>`），`source_id` = `<study>-S<n>`，`condition_id` = `<study>-C<n>`，`experiment_id` = `<study>-E<n>`，`arm_id` = `<study>-A<n>`，`observation_id` = `<study>-O<n>`。
- 审核人修改关联标签后会自动重新挂接。

### 5.3 表

| 表 | 作用 | 关键列 |
|---|---|---|
| `documents` | 原文 | `title`, `source_path`, `text` |
| `segments` | 分段 | `document_id`, `idx`, `start`, `end` |
| `extraction_runs` | 每次 LLM 调用 | `model`, `base_url`, `prompt_version`, `schema_version`, `temperature`, `prompt`, `raw_response`, `status`, `error`（解析说明：缺层、被丢弃的未知字段） |
| `records` | 记录 | `run_id`, `kind`, `parent_id`, `proposed_json`（**不可修改**）, `current_json`, `flags_json`, `status`, `reviewer`, `reviewed_at` |
| `review_log` | 审核日志 | `record_id`, `reviewer`, `action`, `field`, `old_value`, `new_value`, `reason`, `created_at` |

不可变约束由 SQLite 触发器强制执行（与 v1 相同，未改动）：
- `records.proposed_json` 禁止 UPDATE；
- `review_log` 禁止 UPDATE / DELETE；
- `extraction_runs.raw_response` 禁止 UPDATE。

表结构变了，旧的示例字段数据库不兼容：`db.connect` 遇到旧库会报错，提示先改名备份。

### 5.4 记录状态

```
pending ──确认──▶ approved ──重新打开──▶ pending
   │ └──拒绝──▶ rejected ──重新打开──▶ pending
   └──待解决──▶ needs_resolution ──确认 / 拒绝 / 退回──▶ …
```

- `pending` 和 `needs_resolution` 可以修改字段；已确认 / 已拒绝的记录要先重新打开。
- 修改、拒绝、待解决、重新打开必须填写原因；确认的备注可选。
- 每个操作写一条 `review_log`；修改字段时一条日志对应一个被改的属性（值或引文）。

## 6. AI 提取

- **整篇论文一次调用**。只有超过 `MAX_DOC_CHARS`（默认 200,000 字符）的论文才在段落边界切成几块，依次调用：
  - 第 2 块起，提示词里附上已提取的条件（`condition_key`、生物名、株系、组名、温度、阶段），要求复用这些 key，只新增新条件；
  - 论文层只取第 1 块的；后面的块如果又返回论文层，不建记录，写进 run 的 `error`（原始回答里仍在）；
  - 关联本来就在全文档范围解析，同一个 key 出现两个条件时标 `duplicate_key`。
- AI 返回 `{"source": {...}, "conditions": [...], "observations": [...]}`，没写的字段直接省略。提示词里的字段说明和 JSON 示例都由 `fields.py` 自动生成；JSON 示例每层只示范 3–4 个字段。later、系统编号、审核层、系统推出的字段不发给模型。`PROMPT_VERSION = v3`，`SCHEMA_VERSION = microalgae-1`。
- trait_group / metric_code 是 Text 字段，但提示词要求从临时列表（`TEMP_TRAIT_GROUPS` / `TEMP_METRIC_CODES`）里选；列表以外的值标 `not_in_temp_list`（warning）。
- 无论解析是否成功，原始回答都先入库。
  - 输出被截断（`finish_reason = length`）→ `parse_error`，写明“输出被截断”，不自动续写。输出上限由 `LLM_MAX_OUTPUT_TOKENS` 设定（默认 16000）。
  - JSON 不完整 / 格式不对 → run 标记为 `parse_error`，不产生记录（原始回答仍在）。
  - 缺某一层 → 照常入库，并写进 run 的 `error`；最后整篇文档都没有论文层时，系统建一条空的论文记录并标 flag。
  - 关联对不上 → 记录照常入库，`parent_id` 为空，标 flag。
  - 不认识的字段名被丢弃，名字写进 run 的 `error`（原始回答中仍可查到）。

## 7. 自动检查器（flag）

| code | 级别 | 规则 |
|---|---|---|
| `missing_required` | error | Essential 字段为空（电学组的 Essential 只对电学记录检查）；paper_verdict 为 exclude / uncertain 时必须填原因 |
| `quote_missing` | error | 有值但没有引文（分类 / 判定类字段不要求引文） |
| `quote_not_found` | error | 引文在原文中找不到（evidence_excerpt 找不到为 warning） |
| `value_not_in_quote` | warning | 值没有出现在引文里。数字字段按数值比较；verbatim 字段要求原样出现；其他文本只要求其中的数字出现；Choice 不检查 |
| `not_numeric` / `not_integer` | error | Decimal / Integer 字段格式不对 |
| `choice_not_allowed` | error | Choice 字段的值不在临时选项里（`other` / `unspecified` 总是允许）。AI 的原值保留；审核人修改时直接拒绝选项以外的值 |
| `condition_ref_not_found` | error | 观测值指向的条件不存在 |
| `layer_missing` | error | 条件没有论文可挂，或论文记录是系统建的空记录 |
| `ambiguous_ref` / `duplicate_key` / `ref_not_found` | warning | 关联标签有歧义、重复，或 control / comparator 找不到 |
| `value_numeric_mismatch` | error | `value_numeric` 不是 `value_original` 里的数字之一（能识别 ±、范围、科学计数法如 `1.2 × 10⁶`、`1–2 × 10^6`），或 `value_original` 没有原样出现在它的引文里 |
| `not_in_temp_list` | warning | trait_group / metric_code 不在临时列表里 |
| `duplicate` | warning | 论文层、条件层：同一文档中另有一条内容完全相同的同层记录 |
| `duplicate_observation` | warning | 观测值：同一条件 + metric_code + 数值 + 单位 + 时间的另一条观测值（引文不同也算） |
| `missing_vs_sibling` | warning | 同一 experiment 的其他条件填了、这条没填的字段（逐字段标，提示可能漏提取） |

flag 只是提示，不阻止确认。任何修改或状态变化后，整篇文档的关联和 flag 全部重算。

## 8. 网页

- 左侧：记录列表，可按层级、状态、是否有 flag、文档筛选；每条显示层级、系统编号、摘要。
- 右侧：
  - 原文全文，每个字段的引文用不同颜色高亮；该记录所在的分段用浅色底标出。
  - 所属的上级（观测值 → 条件 → 论文）和下级记录，点击即可跳转。
  - 字段按分组折叠；空的可选字段默认隐藏，可以勾选显示。Choice 字段修改时用下拉框（临时选项）。
  - 操作：确认 / 拒绝 / 待解决 / 重新打开。
  - 审核日志；AI 原始回答和解析说明。
- 审核人名字手动填写，存在浏览器 localStorage（无登录）。

## 9. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 字段定义、层级、临时选项字段、版本号 |
| GET | `/api/documents` | 文档列表 |
| GET | `/api/documents/{id}` | 文档全文 |
| GET | `/api/records?status=&document_id=&flagged=&kind=` | 记录列表 |
| GET | `/api/records/{id}` | 记录详情（含高亮位置、flag、上下级、编号、日志、原始提取信息） |
| POST | `/api/records/{id}/approve` | `{reviewer, note?}` |
| POST | `/api/records/{id}/edit` | `{reviewer, field, value?, quote?, reason}` |
| POST | `/api/records/{id}/reject` | `{reviewer, reason}` |
| POST | `/api/records/{id}/flag` | `{reviewer, reason}`（标记待解决） |
| POST | `/api/records/{id}/reopen` | `{reviewer, reason}` |
| GET | `/api/export.csv`、`/api/export.xlsx` | 导出 |

错误：记录不存在 → 404；状态不允许的操作 → 409；参数不合法（含选项以外的值）→ 400。

## 10. 导出格式

每行 = 一条观测值 + 它的条件 + 它的论文，**三层都 approved 才导出**。

列顺序：
1. Extraction Template 的 152 列，有三处调整：`statistice_type` 写成 `statistic_type`；`record_role` 插在 `metric_code` 后面；`mediator_concentration_unit` 插在 `mediator_concentration` 后面；没有字段名的 Morphology 空列去掉。
2. 论文级新字段：`paper_type, paper_verdict, exclusion_reason, uncertain_reason, is_electrical`。
3. 每个需要引文的 AI 字段一列 `<字段>_quote`。
4. 额外列：`condition_source_locator, observation_record_id, condition_record_id, source_record_id, document, edited, model, prompt_version, schema_version`。

空格按字段的“缺失时”一栏填 `Missing` / `N/A`；非电学行的电学字段填 `N/A`；later 字段留空。xlsx 还有一个 `sources` 工作表，列出每篇论文的判定和导出行数（包括被排除的论文）。

## 11. 本版不做

PDF 下载与解析（只留 `documents.load_pdf` 接口）、初筛、MCP、读图、用户登录、`accepted_taxon_name` 与 Availability 组的查询。

## 12. 完成标准

随便在网页上点一条记录，都能看到原文出处和高亮，以及它属于哪个条件、哪篇论文；能确认或修改；能导出与 Extraction Template 列顺序一致、带出处的 Excel。
