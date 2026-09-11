# LLM 分析升级方案 · 岗位报告

> **状态**：v1（2026-09-10 头脑风暴产出，待 review）
> **优先级**（用户确认）：① 画像聚类（**纯 JD**） → ② 匹配度多维核算（待简历） → ③ 求职策略层
> **硬约束**（第一阶段）：
> - **只基于 JD 分析同类岗位的画像与技能要求**——不引入候选人信息（身份/简历/经历）
> - 画像字段全部由 JD 客观可推
> - DB schema 中**不预留** fit_for_me / why_fit / worth_applying 等候选人字段（第二阶段再加）
> - 不堆 CLI 新参数（沿用"零额外参数"原则）

---

## 0. 项目现状摘要（结合 match_report.py / boss_state.py 实际代码）

### 0.1 数据层
- **SQLite** `.boss_profile/boss_state.db`，关键表 `applications` 已含：
  - 业务列：`job_title / company / salary / job_url / city / experience / education / description / status / search_kw / hr_active_label / hr_active_days / created_at / updated_at`
  - **AI 缓存列已预留**：`optimize_result / optimize_at`（24h TTL）、`chat_suggestion_result / chat_suggestion_at`——本次新增表沿用同模式
- `settings` 已有：`ai_api_key / ai_base_url / ai_model / resume_summary / search_keywords / noise_filter_keywords / search_url_history`

### 0.2 LLM 接入（`interview/llm_client.py` 共享）
- `get_embedding(text)` → Ollama `nomic-embed-text`（768 维）
- `llm_chat_ollama(messages, system_prompt, temperature)` → 本地 `qwen2.5:14b`
- `llm_chat_deepseek(messages, system_prompt, temperature=0.3)` → API（settings 读配置）
- `parse_json_from_llm(text)` → 容错 JSON 提取
- `cosine_similarity(a, b)` → numpy 已隐式依赖

### 0.3 报告生成
- `analyze_market(...)` → `build_market_report(...)`：纯统计报告
  - 流程：`_fetch_jobs_with_jd` → `_split_jobs`（search_kw 隔离 + 方向过滤）→ `_is_tech_job`（软件向过滤）→ `parse_skills2` 词频 → `_salary_stats / _dist_table` → 输出
  - 章节：① 市场画像 → ② 技能词 TOP 30 → ③ 分类视图 → ④ 岗位样本 → ⑤-⑧ 各类剔除清单
- `analyze_match(...)` → `build_report(...)`：匹配报告（需 `resume_summary`）
  - 在市场流程上加 `_analyze_llm` / `_keyword_score` 逐岗打分排序
  - `_analyze_llm` 当前 7 字段：`match_score / decision / key_skills / gap / advice / summary / ...`
  - 缓存：`match_cache.json` 扁平 JSON，key = `sha1(url + resume[:500])`
- **文件名**：`年-月-日-关键词（城市）[·软件向][·本次N分钟].md`，**同名覆盖**

### 0.4 CLI（不动）
```
--keywords / --search-kw / --city / --per-query / --max-total
--report-only / --limit / --keyword-only / --market
--include-non-tech   (默认只软件向)
--only-new / --new-since-minutes
```

### 0.5 项目惯例（用户偏好）
- 软件向过滤默认开启（`tech_only=True` 写死，不暴露额外开关）
- 报告同名覆盖（重跑即最新）
- 所有"剔除/过滤"清单完整列出（不截断丢失）
- 一次性数据清洗写 `scripts/`，不堆 CLI 参数
- `search_kw` 宽词 ⊃ 窄词方向性包含（`_expand_search_kws`）
- 关键词缺口分级（🔴≥10 / 🟡≥5 / 🟢<5）已在匹配报告第四节使用——画像聚类沿用同一红黄绿语义

---

## 1. 目标与边界

### 1.1 第一阶段（本文档重点，纯 JD）
**目标**：把"统计型热词报告"升级到"结构化画像型报告"——
- 在薪资/技能词/经验学历统计之外，增加"**岗位画像聚类**"维度
- 让读者一眼看到这个 kw 方向的市场由哪几类典型岗位构成，每类的核心技能 / 门槛 / 典型公司 / 竞争烈度

**明确不涉及**：
- 候选人契合度（fit_for_me / why_fit / worth_applying）
- 简历对比 / 逐岗打分排序 / 单岗画像（这些进第二阶段）
- 任何依赖身份信息、简历的字段

### 1.2 第二阶段（待简历接入）
- 单岗多维雷达（`sub_scores` 7 维）+ 三档差距分级 + 证据引用
- 报告从"匹配度排名"升级到"分层优先级 + 行动建议"

### 1.3 第三阶段（概要）
- 串联画像 + 匹配：冲刺/匹配/保底分层 + 投递优先级 + 能力收敛路线图

---

## 2. 第一阶段：岗位画像聚类（详细设计）

### 2.1 算法选型：A + B 混合

| 步骤 | 方法 | 目的 |
|---|---|---|
| ① Embedding 分群（B） | 复用 `get_embedding` 把每岗 JD（`title + description[:800]`）向量化 → 本地 KMeans 聚类 | 客观、可复现、不烧钱 |
| ② 特征聚合（SQL/统计） | 在 cluster 内做词频、薪资中位数、经验/学历众数、公司频次 | **不靠 LLM 自由发挥**，保证一致 |
| ③ LLM 命名（A） | 把聚合特征 + 代表岗 JD 摘要喂给 `llm_chat_ollama`（纯 JD，**不带简历**），输出画像名 + 客观属性 | 给"人话标签" |

**为什么不选 A 单独**：分群边界随温度抖动、不可复现、样本大时 token 贵。
**为什么不选 B 单独**：分群没有"人话标签"，用户看不懂。

### 2.2 数据流（在 `analyze_market` 末尾插桩）

```
[现有流程，不动]
collect → applications 入库
↓
_fetch_jobs_with_jd(limit, since_minutes, search_kws)     # search_kw 隔离
↓
_split_jobs(jobs, kws, kw_scope, direction)               # 弱相关/方向不符分桶
↓
_is_tech_job 过滤（非软件向剔除）
↓
parse_skills2 词频统计

[新增插桩]
cluster_jobs(jobs, kws, search_kws, since_minutes)
  ├── embed_jobs(jobs)            → 读/写 job_embeddings 缓存
  ├── kmeans_clusters(embeddings) → sklearn KMeans, k = min(5, max(3, n//5))
  ├── build_cluster_payload       → SQL/统计聚合 core_skills / salary_band / ...
  ├── name_cluster(payload)       → llm_chat_ollama 命名 + 客观属性
  └── persist                     → report_clusters 表（按 sample_hash 缓存）

↓
build_market_report(..., clusters)  # 新增"二、岗位画像聚类"章节
↓
写入 reports/年-月-日-xxx.md（同名覆盖）
```

### 2.3 画像 schema（第一阶段，**纯 JD 可推**）

**新表** `report_clusters`：
```sql
CREATE TABLE IF NOT EXISTS report_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date     TEXT NOT NULL,         -- 报告日期 YYYY-MM-DD
    report_kw       TEXT NOT NULL,         -- 分析方向(关键词)
    search_kws_json TEXT NOT NULL,         -- 实际纳入的 search_kw 列表(JSON)
    sample_hash     TEXT NOT NULL,         -- 强相关样本指纹(决定是否重算)
    sample_size     INTEGER NOT NULL,
    clusters_json   TEXT NOT NULL,         -- 画像数组 JSON
    algorithm       TEXT,                  -- 算法版本(例 "kmeans+qwen2.5:14b@2026-09")
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(report_date, report_kw, sample_hash)
);
CREATE INDEX IF NOT EXISTS idx_report_clusters_date ON report_clusters(report_date);
```

**`clusters_json` 数组元素 schema**（第一阶段版，不含任何候选人字段）：
```json
{
  "cluster_id": 1,
  "persona_name": "大厂测开平台型",
  "headcount": 6,
  "ratio": 0.32,
  "salary_band": "30-45K",
  "typical_companies": ["腾讯", "金山办公", "震坤行"],
  "core_skills": ["Python", "CI/CD", "测试平台", "架构设计", "Java"],
  "skill_categories": {
    "技术-开发":      ["Python", "Java"],
    "技术-部署/架构": ["CI/CD", "Linux", "架构设计"]
  },
  "domain": "互联网/工具链",
  "seniority": "senior",
  "threshold_education": "本科",
  "threshold_experience": "3-5年",
  "jd_highlights": [
    "主导质量门禁嵌入 CI/CD 流水线",
    "8年以上互联网开发经验",
    "中大型系统的架构设计能力"
  ],
  "competition_note": "高薪但要求资深开发背景，偏技术专家线；技术深度+体系建设是核心门槛",
  "representative_job_url": "https://www.zhipin.com/job_detail/xxx.html",
  "representative_job_title": "测试开发架构师"
}
```

**字段生成方式**（关键决策，避免幻觉）：

| 字段 | 来源 | 是否调 LLM |
|---|---|---|
| `cluster_id` | KMeans 输出 | ❌ |
| `headcount / ratio` | 聚类成员数 | ❌ |
| `core_skills` | cluster 内 `parse_skills2` 词频聚合 TOP 5-8（复用 SKILL_MAP_V2） | ❌ |
| `skill_categories` | 同上，按 SKILL_MAP_V2 类别聚合 | ❌ |
| `salary_band` | cluster 内 `_parse_salary_k` 取中位数 → 区间 | ❌ |
| `threshold_education / threshold_experience` | cluster 内众数 | ❌ |
| `typical_companies` | cluster 内公司名频次 TOP 3 | ❌ |
| `representative_job_url / _title` | 选离 cluster 中心最近的一岗 | ❌ |
| `persona_name / domain` | LLM 命名 | ✅ |
| `jd_highlights` | LLM 总结 3-5 条 | ✅ |
| `competition_note` | LLM 总结（一句话，**不评候选人**） | ✅ |
| ❌ `fit_for_me` | **第一阶段不输出** | — |
| ❌ `why_fit` | **第一阶段不输出** | — |
| ❌ `worth_applying` | **第一阶段不输出** | — |

> **PR review 红线**：在 `cluster.py` / `match_report.py` 改动部分 grep `fit_for_me|why_fit|worth_applying|resume`（除 prompt 模板外）应为空。

### 2.4 LLM 命名 prompt（**显式不带简历/身份信息**）

```python
CLUSTER_NAMING_PROMPT = """你是岗位市场分析专家。以下是同一搜索方向下一组岗位的代表样本（已按相似度聚为一类），
请你**只基于 JD 客观内容**给出这组岗位的"画像名称"与"客观属性"。

## 输入
- 画像内代表公司: {typical_companies}
- 画像内核心技能(已统计): {core_skills}
- 画像内经验门槛众数: {threshold_experience}
- 画像内学历门槛众数: {threshold_education}
- 画像内薪资带: {salary_band}
- 画像内 JD 摘要(3 个代表岗, 每岗 1-3 句):
{sample_summaries}

## 严格输出 JSON(不要多余文字、不要 markdown 包裹)
{{
  "persona_name": "4-8字画像名, 业内通用词(如'大厂测开平台型'/'金融合规测试型'/'AI Agent评测型')",
  "domain": "所属行业/方向(如'互联网/工具链'/'金融/证券'/'AI Agent评测'/'汽车/座舱')",
  "jd_highlights": ["该画像 3-5 条代表性 JD 要求, 短句, 不要带序号"],
  "competition_note": "客观描述这类岗的门槛/竞争烈度/机会(1-2 句), 不评价任何具体候选人"
}}

## 硬约束
- 只看 JD 客观内容, 不要给"对某类候选人是否合适"之类的判断(那是后续阶段的事)
- 画像名不要带感叹号/情绪词, 不要带"型"以外的口语词
- competition_note 严禁出现"建议你"等指导性措辞, 只写"这类岗..."
- 不要编造公司或技能(只能从"输入"中归纳)
"""
```

调用：`llm_chat_ollama(prompt, temperature=0.2)`——本地 qwen2.5:14b，**无需 API Key**。

### 2.5 embedding 缓存（新表，避免每报告重算）

```sql
CREATE TABLE IF NOT EXISTS job_embeddings (
    job_url        TEXT NOT NULL,
    model          TEXT NOT NULL,           -- 如 "nomic-embed-text"
    embedding_json TEXT NOT NULL,           -- 序列化向量(列表)
    text_hash      TEXT NOT NULL,           -- title+description[:800] 的 sha1, 内容变失效
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_url, model)
);
CREATE INDEX IF NOT EXISTS idx_job_embeddings_hash ON job_embeddings(text_hash);
```

- 读：`SELECT embedding_json FROM job_embeddings WHERE job_url=? AND model=? AND text_hash=?`
- 写：`INSERT OR REPLACE`（内容变 → text_hash 变 → 写新行）
- 旧数据清理：`DELETE WHERE created_at < datetime('now', '-30 days')`

### 2.6 聚类结果缓存策略

- **缓存 key** = `(report_date, report_kw, sample_hash)`
- `sample_hash` = `sha1("\n".join(sorted(job_url_list)))`
- 命中条件：`sample_hash` 相同 → 直接读 `clusters_json`，跳过 embed + kmeans + LLM 命名
- 失效场景：
  - `--refresh` 强制重采 → 样本变 → 重算
  - `--only-new` 配合 `--new-since-minutes` → 样本变 → 重算
  - 算法版本升级（`algorithm` 字段变更）→ 重算
- 清理：`DELETE FROM report_clusters WHERE created_at < datetime('now', '-7 days')`

### 2.7 降级路径（**必须有，没 Key 也要能跑**）

| 失败点 | 检测 | 降级行为 |
|---|---|---|
| `scikit-learn` 不可用 | import 失败 | 跳过整章，报告加一行 `⚠️ 画像聚类需要 scikit-learn，pip install scikit-learn 后重跑` |
| Ollama embedding 不可用 | `get_embedding` 抛异常 | 跳过整章，提示 `启动 ollama serve 后重跑`；其他章节照常 |
| KMeans 样本 < 5 | `len(jobs) < 5` | 跳过整章，提示 `样本不足(n<5)，--per-query 30 重新采集后再试` |
| LLM 命名失败（Ollama 挂） | `llm_chat_ollama` 抛异常 | **仅该 cluster** `persona_name` = "未命名画像 N"，`competition_note` = "聚类可用, LLM 命名暂未生成"；其他 cluster 不影响 |
| JSON 解析失败 | `parse_json_from_llm` 返 None | 同上单 cluster 降级 |
| 既无 Ollama 又无 DeepSeek Key | `_cluster_available()` 返 False | 跳过整章，提示 `启用本地 Ollama 或配置 AI Key 后可解锁画像聚类`；其他章节照常 |

### 2.8 报告呈现（热词报告升级）

**升级前章节顺序**（`build_market_report` 当前输出）：
```
一、市场画像（薪资/经验/学历）
二、热门技能词 TOP 30
三、分类视图(出现于≥20%岗位的技能)
四、岗位样本(应届/校招岗排最后, 其余按HR活跃度)
五、已剔除的弱相关岗位
六、已剔除的方向弱相关岗位
七、已过滤的疑似无关岗位(标题命中黑名单)
八、已过滤的非软件岗位
```

**升级后**：
```
一、市场画像（薪资/经验/学历）       ← 保留, SQL 统计
二、岗位画像聚类 ★新增               ← LLM + embedding, 失败则提示
三、热门技能词 TOP 30                 ← 保留, 作为画像的支撑证据
四、分类视图(出现于≥20%岗位的技能)   ← 保留
五、岗位样本                          ← 保留
六-九、各种剔除清单                   ← 保留(原五-八)
```

**"二、岗位画像聚类"章节 Markdown 模板**：

```markdown
## 二、岗位画像聚类

> 算法: embedding 聚类 + 本地 LLM 命名（**纯 JD 画像，不含候选人信息**）
> 样本: {sample_size} 个强相关岗位 → 聚成 {n_clusters} 类典型画像
> 缓存: {cache_note}        # "首次计算" / "复用 YYYY-MM-DD HH:MM 结果"

| # | 画像 | 样本数 | 占比 | 薪资带 | 主要行业 | 经验门槛 |
|---|------|--------|------|--------|----------|----------|
| 1 | 大厂测开平台型 | 6 | 32% | 30-45K | 互联网/工具链 | 3-5年 |
| 2 | 金融合规测试型 | 3 | 16% | 20-30K | 金融/证券 | 1-3年 |
| ... |

<details><summary>展开 画像 1: 大厂测开平台型（6 岗 · 30-45K · 互联网/工具链）</summary>

- **代表公司**: 腾讯、金山办公、震坤行
- **核心技能**: Python(6)、CI/CD(5)、Java(4)、Linux(4)、测试平台(3)
- **典型 JD 要求**:
  1. 主导质量门禁嵌入 CI/CD 流水线
  2. 8年以上互联网开发经验
  3. 中大型系统的架构设计能力
- **竞争门槛**: 高薪但要求资深开发背景, 偏技术专家线；技术深度+体系建设是核心门槛

| 代表岗位 | 公司 | 薪资 | 链接 |
|---|---|---|---|
| 测试开发架构师 | 金山办公 | 30-45K | [link](https://...) |
| ... |

</details>

<details>画像 2: 金融合规测试型 ...</details>
```

### 2.9 代码改动点（精确到文件/函数）

#### 新增文件 `cluster.py`（项目根，`match_report.py` 同级）
| 函数 | 职责 |
|---|---|
| `embed_jobs(jobs, model="nomic-embed-text") -> dict[url, vec]` | 读/写 `job_embeddings` 缓存；只对缺的调 `get_embedding` |
| `kmeans_clusters(emb_dict, k=None) -> list[list[url]]` | sklearn KMeans；`k` 默认 `min(5, max(3, n//5))` |
| `_cluster_centroid(embeddings, members) -> np.ndarray` | 中心向量计算（供"选代表岗"用） |
| `_pick_representative(emb_dict, members, centroid) -> dict` | 选离中心最近的一岗 |
| `build_cluster_payload(cluster_jobs, freq_global, cats) -> dict` | 纯统计聚合（`core_skills / skill_categories / salary_band / threshold_* / typical_companies`） |
| `name_cluster(payload, sample_summaries) -> dict` | 调 `llm_chat_ollama`，parse JSON，单 cluster 降级 |
| `cluster_jobs(jobs, kws, search_kws, since_minutes) -> list[dict]` | 主入口：缓存命中直接读库；否则 embed → kmeans → payload → name → persist |
| `_cluster_available() -> bool` | sklearn 可用 + (Ollama 跑 或 DeepSeek Key 配了) |
| `_sample_hash(jobs) -> str` | `sha1("\n".join(sorted(url_list)))` |

#### 修改 `boss_state.py`
- `init_db()` 末尾追加（与现有 ALTER 模式一致，外层 try/except）：
  ```python
  try:
      db.execute("""CREATE TABLE IF NOT EXISTS job_embeddings (
          job_url TEXT NOT NULL, model TEXT NOT NULL,
          embedding_json TEXT NOT NULL, text_hash TEXT NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (job_url, model))""")
  except sqlite3.OperationalError: pass
  try:
      db.execute("""CREATE TABLE IF NOT EXISTS report_clusters (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          report_date TEXT NOT NULL, report_kw TEXT NOT NULL,
          search_kws_json TEXT NOT NULL, sample_hash TEXT NOT NULL,
          sample_size INTEGER NOT NULL, clusters_json TEXT NOT NULL,
          algorithm TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(report_date, report_kw, sample_hash))""")
  except sqlite3.OperationalError: pass
  ```
- 新增 5 个 CRUD 函数（签名）：
  - `get_job_embedding(job_url, model, text_hash) -> Optional[list[float]]`
  - `save_job_embedding(job_url, model, embedding, text_hash) -> None`
  - `get_report_clusters(report_date, report_kw, sample_hash) -> Optional[dict]`
  - `save_report_clusters(report_date, report_kw, search_kws, sample_hash, sample_size, clusters, algorithm) -> int`
  - `cleanup_old_clusters(older_than_days=7) -> int`（可选）

#### 修改 `match_report.py`
- `analyze_market(...)` 返回值末尾追加 `clusters: list[dict]`
- 在 `analyze_market` 流程里 `freq.most_common()` 计算后、`return` 前追加：
  ```python
  clusters = []
  if jobs and _cluster_available():
      try:
          from cluster import cluster_jobs
          clusters = cluster_jobs(jobs, kws, search_kws, since_minutes)
      except Exception as e:
          print(f"  [提示] 聚类失败(报告其他部分照常生成): {e}")
  ```
- 新增辅助 `_cluster_available()`（内部直接定义，不依赖 `cluster.py` 以避免循环 import）
- `build_market_report(jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats, clusters=None)`：新增 `clusters` 参数；在"一、市场画像"后插入"二、岗位画像聚类"章节（用 `_render_clusters_section` 辅助函数）
- 章节函数 `_render_clusters_section(clusters, sample_size) -> list[str]`：纯字符串拼装，不调 LLM
- 报告头部说明（`lines.append` 头部行）追加 `· 画像聚类: {n} 类` 或 `· 画像聚类: 暂不可用`

#### 不改动的
- `interview/llm_client.py`（零改动，所有 API 已就绪）
- `boss_firefox.py / boss_company.py / boss_replier.py / lakejob_cli/` / Web 控制台 / 面试子模块
- 报告文件命名规则 / 任何 CLI 参数

### 2.10 CLI 行为（**零新增参数**）

| 现有参数 | 聚类行为 |
|---|---|
| 无（默认） | 检测到 Ollama/DeepSeek → 跑聚类；否则跳过整章加提示 |
| `--keyword-only` | **跳过**聚类（聚类依赖 LLM/embedding，不算"零成本"） |
| `--only-new --new-since-minutes 60` | 样本 hash 变了 → 重算；没变 → 复用缓存 |
| `--refresh` | 强制重采 → 样本必变 → 必然重算 |
| `--include-non-tech` | 不影响聚类（样本已经过软件向过滤，聚类输入不变） |

### 2.11 验收标准

| 项 | 标准 | 验证方法 |
|---|---|---|
| 缓存命中 | 同一 (date, kw, sample_hash) 第二次跑 0 次 embedding 调用、0 次 LLM 调用 | stderr 日志 + sqlite 查询 |
| 缓存失效 | 样本变（增/删岗）后 sample_hash 变 → 自动重算 | 跑两次对比 |
| 降级 | 停 ollama → 重跑 → 报告其他章节照常生成，聚类章节替换为一行提示 | 手动测 |
| 样本不足 | n<5 → 不强行聚类，提示"样本不足" | 制造 n=4 场景 |
| 报告 | 同名文件**直接覆盖**，不 append；含"二、岗位画像聚类"章节时插入正确位置 | 跑 4 份现有报告 |
| 第一阶段零候选人 | grep `cluster.py` + `match_report.py` 改动部分，`fit_for_me\|why_fit\|worth_applying` 应**只**在注释/docstring 出现 | CI grep 检查 |
| 性能 | 单份报告（n=30 岗）冷启动 < 60s（含 embedding + 5 段 LLM 命名）；热启动（缓存命中）< 3s | time 命令 |

### 2.12 依赖

`requirements.txt` 追加：
```
scikit-learn>=1.3.0
```

> 注：项目实际还隐式依赖 `httpx` / `numpy`（`interview/llm_client.py` 已 import）——本次不在改动范围内，但建议下一轮补 `requirements.txt` 完整性（避免单文件复用时踩坑，对应 `MEMORY.md` 已记录的踩坑点）。

---

## 3. 第二阶段：匹配度多维核算（**待简历接入，先出设计**）

> **触发条件**：用户提供 `resume_summary`（已通过 `--resume-file` 导入 settings）

### 3.1 `_analyze_llm` 返回 schema 扩展
```json
{
  "match_score": 78,
  "sub_scores": {
    "tech": 70, "industry": 85, "experience": 90,
    "education": 100, "management": 80, "softskill": 75, "salary": 60
  },
  "decision": "建议投递",
  "gap_level": {
    "hard":           ["要求驻场北京"],
    "patchable":      ["缺K8s部署经验"],
    "bonus_missing":  ["音视频测试经验优先"]
  },
  "evidence": {
    "management": "JD: 主导关键技术方案选型与架构设计(金山办公)",
    "industry":   "JD: 金融/证券测试经验优先(东莞证券)"
  },
  "key_skills": ["Python", "RAG"],
  "resume_advice":        "强调AI测试体系0-1搭建与跨部门协同",
  "interview_narrative":  "用'质量门禁嵌入CI/CD'项目讲透工程+管理双线",
  "worth_applying":       true,
  "summary":              "一两句总结"
}
```

### 3.2 缓存
- 沿用 `match_cache.json`（key = `sha1(url + resume[:500])`，resume 变自动失效）
- 第一阶段 `report_clusters` 缓存不动

### 3.3 报告呈现
- 匹配报告每岗从"分数+决策+一句话 gap"升级到"7 维小表 + 三档 gap 列表 + 证据引用 + 简历改写要点 + 是否值得投"
- 报告顶部新增"**个人能力均值 vs 市场要求均值**"对比（所有匹配岗 `sub_scores` 取均值）

### 3.4 代码改动
- `match_report.py` `_analyze_llm`（438 行）prompt + parse 扩展
- `build_report`（1054 行）改写第三节"岗位详情"模板
- 新增 `settings.llm_subscore_enabled`（默认 `true`）——沿用 settings 模式，可关

### 3.5 验收
- 同一岗同一简历两次跑 → `sub_scores` 一致
- `gap_level` 三档分类覆盖率 ≥ 80%（人工抽检 10 岗）
- `evidence` 字段非空率 ≥ 70%
- 报告头部新增"个人 vs 市场"雷达/均值对比

---

## 4. 第三阶段：求职策略层（**概要**）

- **岗位分层判定**（用第二阶段的 `sub_scores.overall` + `gap_level.hard`）：
  - **匹配**：overall ≥ 80 且 `hard` 为空
  - **冲刺**：60 ≤ overall < 80 或仅有 `patchable`
  - **保底/放弃**：overall < 60 或 `hard` 非空
- **报告输出**：新增"七、求职策略"章节
  - 分层表（按画像分组，附画像 `cluster_id`）
  - 本周投递清单（前 N 个匹配/冲刺岗，按画像 + 薪资排序）
- **代码改动**：`build_report` 末尾追加策略章节；候选池联动已有 `shortlists` 表（schema 不变）
- **触发**：第二阶段稳定后启动

---

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| Ollama 跑大模型慢（5 段命名 × 几秒） | 本地 qwen2.5:14b + `temperature=0.2`；可临时切 DeepSeek（需 Key） |
| KMeans k 选不准 | 自动 k = `min(5, max(3, n//5))`；**第一阶段不加 `--cluster-k` 覆盖**（保持零参数） |
| 画像命名不稳定 | 同温度多次跑取众数（v1 暂不做，先看实际效果） |
| 样本量小（n<5） | 跳过聚类，提示扩采集；不强行聚类出烂结果 |
| embedding 模型升级 | `job_embeddings.model` 字段记录模型版本，升级时按 model 区分缓存 |
| **画像标签与候选人耦合风险** | 第一阶段 schema **不预留** fit_for_me；PR review 时 grep 校验 |
| 隐私 | 画像命名 prompt **显式不带简历**；`competition_note` 禁指导性措辞 |

---

## 6. 落地步骤（按用户优先级）

### 阶段一：画像聚类（纯 JD）—— 拆 4 个小步

| 步骤 | 交付物 | 涉及文件 | 估时 |
|---|---|---|---|
| 1 | 加 `job_embeddings` / `report_clusters` 表 + 5 个 CRUD + `requirements.txt` 加 sklearn | `boss_state.py`, `requirements.txt` | 0.5d |
| 2 | `cluster.py` 全套（embed / kmeans / payload / name / persist / 降级） | `cluster.py`（新） | 1.5d |
| 3 | `analyze_market` 末尾插桩 + `build_market_report` 加画像章节 | `match_report.py` | 1d |
| 4 | 验收：跑 4 份现有报告回归 + 降级场景 + grep 校验 | - | 0.5d |

### 阶段二：匹配度多维核算
- 触发：用户提供 resume_summary 后启动
- 单 PR：`_analyze_llm` 扩展 + `build_report` 重写 + settings 开关

### 阶段三：求职策略层
- 触发：阶段二稳定后启动
- 单 PR：策略章节 + 候选池联动

---

## 7. 修订记录

- **v1 · 2026-09-10** · 头脑风暴产出
  - 确认优先级：① 画像聚类（纯JD）→ ② 匹配度核算 → ③ 策略层
  - 硬约束：第一阶段不含任何候选人字段
  - 详细设计：算法 A+B 混合 / 缓存双层 / 降级全覆盖 / 报告插入"二、岗位画像聚类"
  - 改动范围：新增 `cluster.py`、`boss_state.py` 增表+CRUD、`match_report.py` 末尾插桩；零 CLI 参数；零 breaking change
