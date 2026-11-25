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
| 26 | 读 PDF（PyMuPDF，缺时退回 pypdf；去页眉页脚、合并断行和连字符、替换连字；扫描版报错）；`pdf2txt` 命令；`ingest` 直接接受 .pdf | `documents.py`、`cli.py`、`tests/test_pdf.py` | 完成 |
| 27 | 分开“选论文”和“提取范围”：电学结果只用于判定论文是否入选，入选论文提取全部测量性状和条件；性状列表改为示例；`PROMPT_VERSION = v5` | `extract.py`、`fields.py`、本地规则文件 | 完成 |
| 28 | PDF 原件随文字一起发给模型：`LLM_PDF_INPUT = file_id`（Files API 上传、按 id 引用、提取后删除）/ `inline`（base64）/ `off`；`ingest --pdf`；提示词加读图规则；图中读取的值不要求数字出现在引文里，改标 `graph_value`，并要求 source_locator 写图号；运行记录里存 file_id；`PROMPT_VERSION = v6` | `llm.py`、`config.py`、`extract.py`、`checks.py`、`cli.py`、`tests/test_pdf_input.py` | 完成 |
| 29 | 输出被截断时自动续写（`LLM_MAX_CONTINUATIONS`，默认 2 次），拼接时去掉重开的代码块和重复片段；每次调用的 token 用量存进 `extraction_runs.usage`（旧库自动加列）；命令行显示调用次数和输出 token；`LLM_PDF_INPUT` 默认 `off`；`run.py` 启动器 | `llm.py`、`db.py`、`extract.py`、`cli.py`、`config.py`、`run.py`、`tests/test_llm.py` | 完成 |
| 30 | 续写接缝修补：续写请求原样引用前一段最后 80 个字符；拼接后在接缝处试几个短补丁（如 `":`），只在能让 JSON 明显越过接缝时采用，记录在 usage.seam_repairs；`reparse` 命令用已存的原始回答重新修补、解析，不调用 API（旧运行保持不变，新建一条运行）；改用 `import pymupdf` | `llm.py`、`extract.py`、`cli.py`、`run.py`、`documents.py`、`tests/test_llm.py` | 完成 |
| 31 | `delete-doc` / `run.py delete`：删除文档及其 segments、运行和记录，已有审核记录的拒绝删除；`recheck` 命令按当前规则重算标记（不调用 API） | `extract.py`、`cli.py`、`run.py` | 完成 |
| 32 | 按 2025_YUAN（文档 3，v6）审出的问题修改规则和提示词，`PROMPT_VERSION = v7`，见下表 | `fields.py`、`checks.py`、`extract.py`、`tests/test_v7_rules.py` | 完成 |
| 34 | 换用千问 `qwen3.8-flash`，开启原生 PDF 视觉输入：实测接口支持 `inline`（base64 PDF，按页转图像，约 3,700 输入 token/页），不支持 Files API（`file_id` 返回 404）；`.env` 设 `LLM_PDF_INPUT=inline`、`LLM_EXTRA_BODY={"enable_thinking": false}`；新增自检 `check-llm` / `run.py check`（生成一页只有图片、没有文字层的随机编码 PDF，模型读对才算通过），`run.py add` 在开启 PDF 输入时先自检、不通过就不导入；清空 deepseek-flash 的旧提取（备份在 `data/backups/`） | `llm_check.py`、`cli.py`、`run.py`、`.env.example`、`tests/test_llm_check.py` | 完成 |
| 35 | `LLM_PDF_INPUT=images`：本地把 PDF 页渲染成 PNG（`pdf_images.py`），图片和提取出的文字分开发送，每张图前标 `[PDF page N]`，文字放在最后；默认只发有图注 / 嵌入图片 / 矢量图的页（`LLM_IMAGE_PAGES=figures`，VIC 6/10 页、KUR 15/18 页），150 dpi（实测 72 dpi 读错柱顶数字，110 / 150 正确；约 2,100 token/页，整份 PDF 直传约 3,700）；提示词在图片模式下加一段 PAGE IMAGES 说明。`LLM_CACHE_CONTROL=1`：system 提示词和 user 最后一块加 `cache_control`，续写时前缀不变（实测千问第二次调用 19,613 输入命中 19,578）；关掉时请求和以前完全一样 | `pdf_images.py`、`llm.py`、`config.py`、`extract.py`、`.env.example`、`tests/test_page_images.py` | 完成 |
| 36 | 按 VIC（v8，qwen3.8-flash）的核对结果改 `PROMPT_VERSION = v9`，见下表 | `extract.py`、`checks.py`、`review_service.py`、`tests/test_v9_rules.py` | 完成 |
| 37 | 按 VIC v9 的核对结果改 `PROMPT_VERSION = v10`，新增不依赖模型的召回检查（`recall.py`、`run.py recall`），修 ACS 页眉混进正文，见下表 | `extract.py`、`fields.py`、`recall.py`、`review_service.py`、`documents.py`、`cli.py`、`run.py`、`tests/test_v10_rules.py`、`tests/test_recall.py`、`tests/test_pdf.py` | 完成 |
| 38 | 按 VIC v10、KUR v10 的结果改 `PROMPT_VERSION = v11`，见下表 | `extract.py`、`fields.py`、`checks.py`、`review_service.py`、`tests/test_v11_rules.py` | 完成 |

### v7 修改与对应问题（2025_YUAN，文档 3）

| 编号 | 问题（v6 上的表现） | 修改 | 生效方式 |
|---|---|---|---|
| R1a | 纯归类标签没有引文被报错：`organism_group`、`genotype_status`、`condition_phase`、`light_measurement_basis`、`biomass_loading_basis`（约 60 处） | 这些字段和全部 `*_basis` 加进 `NO_QUOTE`，**暂定，待 Carly 确认**（FIELDS.md 问题 21）。`*_unit` 仍要求引文 | 规则，`recheck` 即生效 |
| R1b | `value_numeric` 没有引文被报错（6 处） | `value_numeric` 沿用 `value_original` 的引文（`QUOTE_FROM`）；数值一致性仍由 `value_numeric_mismatch` 检查 | 规则，`recheck` 即生效 |
| R2 | 必填字段原文没写也报错：`feed_mode` 12 处、`mediator` 7 处，分不清"原文没写"和"AI 漏了" | 每条记录新增 `not_stated` 列表：列了的降为警告 `not_stated`（导出仍为 Missing），没列也没填的仍是错误；`mediator = none` 只在原文明说未加介体时才填；不允许用 `unspecified` 代替 | 提示词 + 规则，要重新提取 |
| B1 | 悬浮模式条件（C7–C10）套用了生物膜模式的 ITO 电极 | 提示词：装置参数不能跨模式 / 装置 / 实验套用，除非原文说明是共用的 | 提示词，要重新提取 |
| B2 | O20 "10–30% higher" 用了绝对量 `photocurrent_density`，`value_original` 不是原样摘抄 | 提示词：相对差值的 `value_original` 原样摘抄，比较写进 `comparison_description`，`metric_code` 不用绝对量 | 提示词，要重新提取 |
| B3 | ITO 引文少了括号（10 处），O25 `evidence_excerpt` 把不相邻的两段拼在一起 | 提示词：引文必须是原文中连续的一段，逐字照抄，括号和缩写都要保留 | 提示词，要重新提取 |

文档 3 保留为 v6 基线，不重新提取。`recheck 3` 后：错误 123 → 62，警告 37 → 37。v7 另外跑一份，再比较两版的错误数。
注意：这篇不在 Paper Distribution 的 11 篇里，没有人工提取结果，只能用来调提示词；和人工结果的正式对比要用已经人工提取过的论文（如 2014_NG、2017_NG）。

### 33：传输与输出格式（v8，按 KUR / VIC 两篇的 token 分析）

| 发现（v7，DeepSeek） | 修改 |
|---|---|
| 第一次调用的 32k 输出里 77–85% 是隐藏推理（只出了 1.6–2.5 万字符正文），推理撑满上限导致续写 | `LLM_REASONING_EFFORT` / `LLM_EXTRA_BODY` 可关闭或降低推理（参数随服务商）；`LLM_JSON_MODE` |
| 缓存命中、推理 token 没有记录，无法核对 | 每次调用存完整 usage，另取 `cached_tokens`（OpenAI / DeepSeek 两种写法）和 `reasoning_tokens`；`python run.py usage [文档号]` 报告累计 API 输出与最终保存的有效输出 |
| 保存的去重有效内容只占 API 输出的 7–8%；条件字段原样重复占原始 JSON 36–44%；同一引文重复抄写 | 输出改为紧凑格式：`quotes` 表（每段引文写一次，字段用 `["值", "q3"]` 引用），`condition_templates`（共用设置写一次，条件只写差异）；解析时展开成原来的记录，数据库、网页、导出不变；旧格式照常解析 |
| 表格行漏提（KUR Table 1） | 规则：结果表的每个数值单元格各记一条观测值 |
| 一句话覆盖多个对象只记一个（Fp/Pht 叶绿体数、Tw/Cc 脱落比例） | 规则：每个对象各记一条，共用引文编号 |
| 设定值当成结果（O71：250 μM 记成光电流） | 规则：作者设定的浓度、光强、负载等属于条件；单位必须和 metric 对得上 |
| "<1" 被填成数值 1 | 规则：带 < > ≤ ≥ 的值 value_numeric 留空（待 Carly 确认） |
| value_original 有一半没有引文 | 规则：value_original、unit_original 必须带引文编号（编号比整句便宜） |

`PROMPT_VERSION = v8`。文件：`llm.py`、`config.py`、`extract.py`、`usage_report.py`、`cli.py`、`run.py`、`.env.example`、`tests/test_v8_compact.py`。


### 36：v9 修改与对应问题（2024_VICENTE-GARCIA，v8，文档 1）

| 编号 | 问题（v8 上的表现） | 修改 | 生效方式 |
|---|---|---|---|
| 1 | 一个条件只能引用一个模板，提示词却建议"培养一个模板、装置一个模板"，10 个生物电极条件丢了物种和培养信息（必填缺失 75） | `"template"` 可以是列表，按顺序合并（后面的覆盖前面的）；合并后仍没有物种名，单独报 `no_organism`；删掉矛盾的那句 | 解析 + 提示词，要重新提取；检查已生效 |
| 2 | 大量字段只写值不带引文编号（`quote_missing` 308），模板里的数值也没有出处；15 个条件都缺 `condition_source_locator` | 记录和模板可以写默认引文 `"quote": "qN"`，只写值的字段自动用它（不要求引文的字段不受影响）；`condition_source_locator` 写进模板 | 解析 + 提示词，要重新提取 |
| 3 | 图 3b/3c/3d 的系列值（16 个）全挂在 Pht_bioanode 下，丢了光强 / 细胞密度 / 介体浓度 | 规则：系列里每个点各建一个条件，写明变化的设置；看不出 x 值就不从图上读。检查器新增 `series_without_x`：同一条件、同一指标、同一时间下有多个不同的图读值就报警告（v8 上报了 17 处） | 提示词要重新提取；检查已生效 |
| 4 | Pht 脱落比例用了总体那句 "<2.5%"，没用专门说 Pht 的 "fewer than 1%" | 规则：同一对象既有专门说法又有总体说法时，用专门说它的那句 | 提示词，要重新提取 |
| 5 | 灭活对照的值写成 "same result" | 规则：指代要还原成实际的值 | 提示词，要重新提取 |
| 6 | "~100 nA/cm2" 的 value_numeric 填成 0.1（私自换算） | 规则：value_numeric 必须是 value_original 里原样写出的数字（检查器原有 `value_numeric_mismatch`） | 提示词，要重新提取 |
| 7 | Tw_bioanode 和 Tw_bioanode_500uM 完全相同 | 规则：相同的条件不重复建；检查器比较条件是否重复时忽略 `condition_key`（v8 上报了 6 处） | 提示词要重新提取；检查已生效 |

v8 基线（文档 1，按 v9 的检查规则重新检查后）：错误 388、警告 90；必填缺失 65 + 无物种名 10、`quote_missing` 308、`series_without_x` 17、条件重复 6；对上 Yuntian 18/30（FDA 两条缺失）。


### 37：v10 修改与对应问题（2024_VICENTE-GARCIA，v9，文档 2）

| 编号 | 问题（v9 上的表现） | 修改 | 生效方式 |
|---|---|---|---|
| 1 | 非电学性状整类消失：叶绿体数、脱落比例（v8 各 4 条，v9 0 条），FDA 两版都没有 | 提示词：正文里写到的每个测量性状都要提，系列规则只增加观测值，不能替代 | 要重新提取 |
| 2 | 模板用一句话当默认引文，盐度、体积、pH、检测温度都套用它（`value_not_in_quote` 约 300）；`organism_name_reported` 写成全名 | 默认引文只允许用在观测值上，模板和条件里的 `"quote"` 不再生效并记入解析说明；`organism_name_reported` 必须照抄（如 "P. tricornutum"），不展开缩写。**全名放进 `accepted_taxon_name` 没有做**：这个字段是 later（要对照分类数据库，FIELDS.md 问题 11），AI 写的全名不等于核实过的学名 | 解析已生效（reparse 可用）；提示词要重新提取 |
| 3 | 漏提要等到和人工表对比才发现 | **召回检查**（不调用 API）：扫描 Results / Discussion 里带"数字 + 单位 / %"、"∼"、"±"的句子，没有被任何观测值引文（或 evidence_excerpt）覆盖的，在论文记录上报 `uncovered_result` 警告；`python run.py recall <文档号>` 打印覆盖率和句子清单。图注（"Figure n." / "Fig. n 大写词" 开头的段落，不含正文里的 "Figure 3d shows"）、标题和坐标轴标签（不到 60 字、没有句末标点的段落）、只写设置值的句子（"was set at"、"λex"、"stored" 等方法用语，且没有 % ± ∼）不计入分母、不报警告，单独计数 | 检查，`recheck` 即生效 |
| 4 | `group_type` 是 AI 自己起的组名，原文没有对应句子（`quote_missing` 40） | 加进 `NO_QUOTE`，**暂定，待 Carly 确认**（FIELDS.md 问题 21）；`analyte` 仍要引文 | 规则，`recheck` 即生效 |
| 5 | `immobilization_method` 引文找不到 39 处 | 查原始回答：引文和原文一字不差，是**我们的 PDF 文字**出错——ACS 把期刊名、网址、"Research Article"、DOI、页码放在每页文字流最后 6 行，旧代码只看最后 2 行，页眉插进了跨页的句子。页眉检查窗口扩到首尾各 6 行（最多一页的三分之一）。跨页句子不合并（双栏论文每页最后一块常是图注，合并会把正文粘到图注后面）；引文匹配本来就把空白当一个空格，跨页引文照样能找到 | 要重新导入（PDF 转文字）；KUR、life 文字不变，VIC 去掉 892 字符页眉 |

### 各版对比（VIC，qwen3.8-flash，images 模式，同一套 v10 检查规则）

| 版本 | 调用 | 输入 / 输出 token | 条件 / 观测值 | 错误 | 必填缺失 + 无物种名 | `quote_missing` | `series_without_x` | 条件重复 | 结果句覆盖（`recall`） | 对上 Yuntian |
|---|---|---|---|---|---|---|---|---|---|---|
| v7（DeepSeek，纯文字） | — | — | — | — | — | — | — | — | — | 19/30；有效输出 8% |
| v8 | 1 | 34,593 / 14,818 | 15 / 46 | 373 | 65 + 10 | 293 | 17 | 6 | 9/21（43%） | 18/30 |
| v9 | 1 | 35,011 / 16,089 | 40 / 45 | 175 | 7 + 0 | 126 | 0 | 0 | 7/21（33%） | 待对比（叶绿体数、脱落比例消失） |
| v10 | 1 | 34,749 / 22,766 | 61 / 47 | 2,540 | 0 + 0 | 2,486（模板字段按条件重复计数；去重后约 131 个模板字段 + 166） | 0 | 4 | 12/21（57%） | 待对比（叶绿体数、脱落比例、FDA 都回来了） |
| KUR v10 | 1 | 58,044（缓存命中 7,932）/ 35,839 | 144 / 70 | 2,201 | 140（70 条观测值都缺 extraction_method、value_status） | 1,785（同上，模板重复计数） | 0 | 80 | 30/44（68%） | 待对比；95 个条件没有观测值 |

结果句覆盖的分母不含图注、标题 / 坐标轴标签和只写设置值的句子（VIC 36 句里排除 7 句图注、8 句设置句，剩 21 句；`recall` 会列出排除的数量），各版之间可以直接比。v7 是 DeepSeek 纯文字模式，数据库已清空，只留下 PLAN #33 里的对上条数和有效输出占比。v9 的 `quote_not_found` 45 有一部分是旧 PDF 文字的页眉问题（v10 重新导入后消失）。下一步：VIC 和 KUR 一起跑 v10，两篇都不倒退才算改进；留 2017_NG 不参与调整，v10 稳定后再拿它测。


### 38：v11 修改与对应问题（VIC v10 文档 3、KUR v10 文档 4）

| 编号 | 问题（v10 上的表现） | 修改 | 生效方式 |
|---|---|---|---|
| 1 | 图上的点被补齐、横坐标按等间距假设 | 读图规则：看得见几个点就记几条，绝不补点；x 值对着坐标轴读近似值（如 "~3.8"），单位照坐标轴原样写，不按等间距假设 | 提示词，要重新提取 |
| 2 | 无法自动发现编造的系列 | 检查器 `suspected_fabricated_series`：同一图、同一指标的图读值，若它们条件里变化的那个数值字段是完美等差数列，且有 3 个以上连续相同的 y 值，就报警告（VIC v10：细胞数系列 11 处） | 检查，`recheck` 即生效 |
| 3 | `metric_code` 用了 other（VIC 10 条），和人工表的指标名对不上 | 提示词里 metric_code 不再列 other；检查器 `metric_other` 报错。临时列表加入人工表用的名字：short_circuit_current、load_voltage、load_current、power、max_power、photocurrent_retention、internal_quantum_efficiency（电学），chloroplast_count、detached_cell_fraction，**暂定，待 Carly 确认**（FIELDS.md 问题 26） | 提示词要重新提取；检查已生效 |
| 4 | KUR 的 Pmp（µW）记成 max_power_density | 规则：单位决定指标，功率单位只能是 power / max_power，带面积的才是 density。检查器 `unit_metric_mismatch`：密度指标的单位没有面积、或功率 / 电流指标的单位带面积，都报错（KUR v10：9 处） | 提示词要重新提取；检查已生效 |
| 5 | 模板字段缺引文被每个条件重复计数（VIC 2,320、KUR 1,440） | 解析时记下每个字段来自哪个模板（`from_template`）；同一模板字段的引文问题只在第一个条件上报一次，注明另有几个条件；审核人改了这个字段后它就不再算模板字段，下一个条件接着报 | 新提取的结果生效（v10 的旧结果没有来源信息） |
| 6 | KUR 144 个条件里 95 个没有观测值 | 检查器 `condition_without_observation` 警告；提示词和示例改为先写观测值、再写它们用到的模板和条件 | 检查已生效；顺序要重新提取 |
| 7 | KUR 70 条观测值都缺 extraction_method、value_status | 提示词明确要求每条观测值都写这两个字段，示例里也加上 | 要重新提取 |
| 8 | 参照组挂到具体编号的单元上；正文和表格重复记 | 规则：参照组（如 "single µPSC"）单独建条件；同一个值在正文和表格里都出现时只记一条 | 要重新提取 |

## 下一步（需要你决定）

1. 拿到 Carly 的答复后（见本地 `docs/FIELDS.md` 末尾的待确认问题，暂时不公开），替换 `TEMP_CHOICES`、电学判定清单，`SCHEMA_VERSION` 加一。
2. 用真实 API key 跑一篇论文，看提取质量。
3. 之后再排：PDF 解析、初筛、读图、MCP。
