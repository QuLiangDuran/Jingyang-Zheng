# 开发计划（v1）

| # | 步骤 | 产出 | 状态 |
|---|---|---|---|
| 1 | 项目骨架、配置、依赖 | `requirements.txt`、`.env.example`、`audit/config.py` | 完成 |
| 2 | 字段定义 + 记录 schema | `audit/fields.py` | 完成 |
| 3 | 文档载入、分段；PDF 接口占位 | `audit/documents.py` | 完成 |
| 4 | 原文定位（归一化匹配 → 字符偏移） | `audit/textmatch.py` | 完成 |
| 5 | SQLite 建表 + 不可变触发器 | `audit/db.py` | 完成 |
| 6 | LLM 客户端（OpenAI 兼容 + 回放） | `audit/llm.py` | 完成 |
| 7 | 分段提取，原始回答入库 | `audit/extract.py` | 完成 |
| 8 | 自动检查器 | `audit/checks.py` | 完成 |
| 9 | 审核服务层 + 审核日志 | `audit/review_service.py` | 完成 |
| 10 | 导出 CSV / Excel | `audit/export.py` | 完成 |
| 11 | FastAPI 接口 | `audit/api.py` | 完成 |
| 12 | 网页 | `audit/static/` | 完成 |
| 13 | 命令行 + 演示数据 | `audit/cli.py`、`samples/` | 完成 |
| 14 | 测试 | `tests/` | 完成 |

## v1.1：换成 MicroalgaeMatch 真实字段（2026-11-24）

| # | 步骤 | 产出 | 状态 |
|---|---|---|---|
| 15 | 字段定义：154 个原表字段 + 5 个论文级字段 + 9 个关联字段；临时选项集中在 `TEMP_CHOICES` | `audit/fields.py` | 完成 |
| 16 | 三层记录（同一张 records 表加 `kind` / `parent_id`），新状态 `needs_resolution`；触发器未动 | `audit/db.py` | 完成 |
| 17 | 解析三层回答；提示词字段说明和 JSON 示例自动生成，规则文字不变；`PROMPT_VERSION = v2` | `audit/extract.py` | 完成 |
| 18 | 检查器：类型、临时选项、电学必填、论文判定 | `audit/checks.py` | 完成 |
| 19 | 关联解析、系统编号、整篇重算 flag、待解决 | `audit/review_service.py` | 完成 |
| 20 | 导出：模板列顺序 + 论文级字段 + 引文列；三层都确认才导出；sources 工作表 | `audit/export.py` | 完成 |
| 21 | 网页：层级筛选、分组折叠、上下级跳转、下拉选项、待解决按钮 | `audit/static/` | 完成 |
| 22 | 虚构微藻示例 + 测试（40 个） | `samples/`、`tests/` | 完成 |
| 23 | 高亮吞字修复；duplicate_observation、missing_vs_sibling、value_numeric_mismatch | `static/`、`checks.py`、`review_service.py` | 完成 |
| 24 | 整篇一次提取（超长才分块，后续块复用 condition_key）；截断标 parse_error；规则 3 改为省略；JSON 示例精简；trait/metric 临时列表；`PROMPT_VERSION = v3` | `extract.py`、`llm.py`、`config.py`、`fields.py` | 完成 |
| 25 | 合并阅读规则（本地 `docs/extraction_rules_prompt.txt`）；`strain_id` 由系统复制；新增 `potential_applied`；`PROMPT_VERSION = v4`，`SCHEMA_VERSION = microalgae-2` | `extract.py`、`fields.py`、`checks.py`、`export.py` | 完成 |

## 下一步（需要你决定）

1. 拿到 Carly 的答复后（见本地 `docs/FIELDS.md` 末尾的待确认问题，暂时不公开），替换 `TEMP_CHOICES`、电学判定清单，`SCHEMA_VERSION` 加一。
2. 用真实 API key 跑一篇论文，看提取质量。
3. 之后再排：PDF 解析、初筛、读图、MCP。
