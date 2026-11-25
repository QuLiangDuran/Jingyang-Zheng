# 文献数据提取与人工审核

AI 从论文文字中提取 MicroalgaeMatch 数据（论文 → 条件 → 观测值三层）→ 自动检查打 flag → 审核人在网页上看原文高亮、确认 / 修改 / 拒绝 → 导出与 Extraction Template 列顺序一致、带出处的 Excel。

设计见 [docs/DESIGN.md](docs/DESIGN.md)，计划见 [docs/PLAN.md](docs/PLAN.md)，字段清单和待确认问题见 [docs/FIELDS.md](docs/FIELDS.md)。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # 然后填 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
```

## 使用

```powershell
# 不需要 API key：载入虚构示例论文和预置的 AI 回答（其中几条故意有错，用来演示 flag）
.\.venv\Scripts\python -m audit demo

# 导入一篇纯文本论文并调用 AI 提取
.\.venv\Scripts\python -m audit ingest path\to\paper.txt --title "论文标题"

# 打开审核网页 http://127.0.0.1:8000
.\.venv\Scripts\python -m audit serve

# 命令行导出（网页右上角也能导出）
.\.venv\Scripts\python -m audit export exports\approved.xlsx
```

数据库默认在 `data/audit.db`。想清空，删掉这个文件即可。旧的示例字段数据库已备份为 `data/audit.example-1.db`，和现在的结构不兼容。

## 修改提取字段

只改 [audit/fields.py](audit/fields.py)：提示词、解析、检查器、网页、导出都会跟着变。改完把 `SCHEMA_VERSION` 加一。Choice 字段的临时选项都在同一个文件的 `TEMP_CHOICES` 里（待 Carly 确认）。

## 测试

```powershell
.\.venv\Scripts\python -m pytest
```
