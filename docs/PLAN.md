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

## 下一步（需要你决定）

1. **改提示词**（单独一步）：用 `docs/extraction_rules_prompt.txt` 重写 `extract.py` 的规则文字，`PROMPT_VERSION` 再加一。建议同时改成**整篇论文一次提取**：条件写在 Methods、结果写在 Results，分段调用时 AI 很难把两者对上；现在的提示词约 2.6 万字符，每段都要重复发送。
2. 拿到 Carly 的答复后（见 [FIELDS.md](FIELDS.md) 末尾的待确认问题），替换 `TEMP_CHOICES`、电学判定清单，`SCHEMA_VERSION` 加一。
3. 用真实 API key 跑一篇论文，看提取质量。
4. 之后再排：PDF 解析、初筛、读图、MCP。
