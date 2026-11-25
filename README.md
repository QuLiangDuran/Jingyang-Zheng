# 文献数据提取与人工审核

AI 从论文文字中提取 MicroalgaeMatch 数据（论文 → 条件 → 观测值三层）→ 自动检查打 flag → 审核人在网页上看原文高亮、确认 / 修改 / 拒绝 → 导出与 Extraction Template 列顺序一致、带出处的 Excel。

设计见 [docs/DESIGN.md](docs/DESIGN.md)，计划见 [docs/PLAN.md](docs/PLAN.md)。字段清单和待确认问题见本地 `docs/FIELDS.md`（暂时不公开，不在仓库里）。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # 然后填 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
```

API key 只写在项目根目录的 `.env` 里（`.gitignore` 已排除，不会提交）：`LLM_API_KEY` 填 key，`LLM_BASE_URL` 填服务商的 OpenAI 兼容地址，`LLM_MODEL` 填模型名。

PDF：用 PyMuPDF 读文字层（已在 requirements.txt）；扫描版 PDF 没有文字层会报错，不做 OCR。有 PMC 网页全文的论文，复制网页正文存成 .txt 通常更干净。

把 PDF 原件也发给模型（`.env` 里的 `LLM_PDF_INPUT`，默认 `off`；DeepSeek 不支持，保持 `off`）：

| 值 | 做法 |
|---|---|
| `file_id` | 通过服务商的 Files API 上传，消息里只引用 file_id（OpenAI）；提取完自动删除 |
| `inline` | 把 PDF 以 base64 放进消息（没有 Files API 的服务商） |
| `off` | 只发文字 |

模型同时看到 PDF 页面和提取出的文字：引文仍必须从文字里逐字复制（网页高亮靠它）；只在图里出现的值记为 `extraction_method = graph_estimated`、`source_locator` 写图号、引文用图注，系统会标 `graph_value` 提醒审核人对照原图。需要支持 PDF 输入的模型。

## 快速启动（run.py）

在 PowerShell、cmd 或 Git Bash 里都可以用；会自动使用项目的 `.venv`。

```bash
python run.py                                  # 打开审核网页（已在运行就直接打开浏览器）
python run.py add papers/life-15-00299.pdf     # 导入并提取一篇或多篇论文，然后打开网页
python run.py add papers/x.txt --pdf papers/x.pdf --title 2025_YUAN
python run.py reparse 3                        # 用已存的 AI 原始回答重新解析文档 3（不调用 API）
python run.py recheck 3                        # 按当前规则重新检查文档 3 的标记（不调用 API，内容不变）
python run.py delete 1 2                       # 删除文档 1、2 及其 AI 回答和记录（已有审核记录的拒绝删除）
python run.py reset                            # 删除所有文档和记录（会先确认；先停掉网页）
python run.py usage 1                          # 文档 1 的 token 报告：缓存命中、推理、累计输出 vs 保存的有效输出
python run.py check                            # 检查模型能不能把附带的 PDF 当图像读（一次很小的 API 调用）
python run.py test                             # 跑测试
python run.py demo                             # 载入虚构示例论文
```

## 使用

```powershell
# 不需要 API key：载入虚构示例论文和预置的 AI 回答（其中几条故意有错，用来演示 flag）
.\.venv\Scripts\python -m audit demo

# 先把 PDF 转成文本检查一下（AI 看到的就是这份文本；图里的数值不会出现）
.\.venv\Scripts\python -m audit pdf2txt papers\2014_NG.pdf

# 导入论文（.pdf 或 .txt）并调用 AI 提取；.pdf 会自动作为原件附给模型
.\.venv\Scripts\python -m audit ingest papers\2014_NG.pdf --title "2014_NG"

# 用改过的 .txt 导入，同时附上 PDF 原件
.\.venv\Scripts\python -m audit ingest papers\2014_NG.txt --pdf papers\2014_NG.pdf --title "2014_NG"

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
