# 企业私有知识库问答 POC（基于 WeKnora 二次开发）

> 场景来源：智途人工智能（青岛）有限公司 · AI 应用实习（2026.07 – 2026.09）· 企业制度与产品文档知识库
> 技术基座：腾讯 [WeKnora](https://github.com/Tencent/WeKnora)（MIT）· 自研部分见下文「我做了什么」

面向**企业制度 + 产品文档**的私有知识库问答，目标是先跑通一条能在内网落地的链路：
文档清洗 → 条款感知分块 → 入库（体检/版本/密级三道闸）→ 检索问答 → **边界评测** → **回归门禁** → 异常治理。

这个仓库不是 WeKnora 的镜像，也不包含上游代码：WeKnora 通过 `deploy/` 下的 compose 文件
以官方镜像方式部署，本仓库只放**二次开发与交付资产**——这也是实习里真正由我产出的部分。

---

## 一、我做了什么（与上游的边界）

| 模块 | 内容 | 是否自研 |
|---|---|---|
| `pipeline/` | 清洗、条款感知分块、文档体检、版本时效治理、密级准入、批次台账、表格结构化、旧块清理、三种入库模式 | ✅ 自研 |
| `eval/` | 边界用例集、双层指标（召回率@k/MRR + 要点命中）、离线基线、**badcase 回流**、**回归门禁** | ✅ 自研 |
| `client/` | API 客户端：超时、指数退避、限流、熔断、按模式幂等、上传重试完整性 | ✅ 自研 |
| `ops/` | 上线自检（连通性/积压/空召回/延迟/批次）+ **发布自检** + **业务指标（分部门/高频问题）** + 排查手册 | ✅ 自研 |
| `deploy/` | 内网部署编排：模型/向量库指向可控地址、资源限额 | ✅ 自研（基于上游 compose） |
| 解析、检索、问答、前端 | 文档解析、混合检索、LLM 问答、管理界面 | ❌ 上游 WeKnora 提供 |

**为什么这么做**：企业侧的难点不在"把 RAG 跑起来"，而在**把什么样的文档、切成什么样、
怎么证明它答得对、答不出来时怎么不瞎答、谁能看、哪一版算数、出问题时怎么定位**。
上游解决前者，这几条是本仓库的交付物。

> ⚠️ 仓库内**不含任何企业真实文档**。`data/sample/` 是按真实文档结构脱敏重写的样例，
> 用于演示清洗与分块规则；评测用例也是脱敏后的等价问法。
> 本仓库是该场景下按交付标准整理的工程实现，文档、用例与代码均已脱敏，
> 企业侧的实际部署细节不在其中。

---

## 二、链路

```
企业文档（docx/pdf/md）
   │
   ├─ pipeline/cleaner.py    去页眉页脚/水印/页码，全角数字归一，条款编号规范化
   ├─ pipeline/chunker.py    按「章-节-条」层级切分，块头注入条款路径，表格不切，
   │                         合并时把条款号写回正文，每块生成稳定 uid
   ├─ pipeline/quality.py    文档体检：给分 + 给业务部门看得懂的整改建议
   ├─ pipeline/versioning.py 同一文件编号多版本？只留"当前有效"的那版（支持 --as-of）
   ├─ pipeline/security.py   密级 vs 知识库准入；检索后按人过滤；越权话术与无答案一致
   ├─ pipeline/tables.py     表格结构化：数值题走区间查询（"8 天及以上"能匹配 9 天）
   ├─ pipeline/ingest.py     三道闸 → 落盘（cleaned/chunks）→ 上传 → 记批次台账
   ▼
WeKnora（官方镜像：解析 / 向量化 / 混合检索 / LLM 问答）
   ▲
   ├─ client/weknora.py      统一出口：超时 30s、5xx/429 指数退避、QPS 限流、连续失败熔断
   ├─ client/responses.py    评测与自检共用同一套"响应怎么解析"的口径
   ├─ eval/runner.py         用例 → 检索/问答 → 指标 → CSV + Markdown 报告（可按角色过滤召回块）
   ├─ eval/offline.py        不起服务，纯本地复算召回率@k / MRR / 要点命中
   ├─ eval/badcase.py        线上反馈 → 候选用例 → 人工复核表 → 并入用例集
   ├─ eval/regression.py     和基线比，退化即退出码 1（发布门禁）
   └─ ops/insights.py        交互日志 → 首次解决率/转人工/重复提问/单次成本
```

## 三、分块为什么不是"每 500 字切一刀"

制度类文档的检索单元是**条款**，不是段落。块切错了，后面再好的模型也救不回来：

1. **层级前缀**：每块开头带上 `第X章 > 第X节 > 第X条` 的路径，让"请假流程"这种短块
   也带上下文，向量检索不再只靠正文猜；
2. **表格不切**：金额档位、权限矩阵一旦被切断就失去意义，整表进一个块；
3. **条款优先**：先按条款边界切，超长条款再按句子切，绝不跨条款；
4. **短块合并**：相邻且同属一条的碎块合并，减少"一条半句话"被召回的噪声；
5. **合并要把条款号写回正文**：两个块合一块后，块头路径只剩第一个条款号，后面那条就"没有出处"了
   ——用户问"第10条怎么规定"时既召不回、引用还会指错。多写四个字，换来条款级引用不串号；
6. **稳定块 uid**：`doc_id + 层级路径 + 正文` 的指纹。重入库时"哪些块真的变了"能算出来，
   而不是靠 index 猜；
7. **表格另走一条路**：整表入块保证"看得见"，但数值题（"出差 9 天谁审批"）不让模型读表硬算，
   抽成结构化记录后用区间查询（`pipeline.tables`）——错一次金额就是一次报销事故。

规则与取舍写在 [`docs/02-文档清洗与分块规范.md`](docs/02-文档清洗与分块规范.md)。

## 四、评测怎么算"答得对"

评测不是看几条回答像不像，而是分层、可复算的指标（[`docs/03-边界评测体系.md`](docs/03-边界评测体系.md)）：

| 指标 | 口径 | 为什么重要 |
|---|---|---|
| **召回率@k / MRR** | 预期条款是否进前 k、排第几 | 先分清"块没捞到"还是"捞到了但答错" |
| **命中率** | 答案包含该用例的必答要点关键词 | 最直接的正确性 |
| **引用命中率** | 返回的引用里出现预期文档/条款 | 防"答对了但出处是瞎编的" |
| **拒答率** | 无答案用例被正确判为"资料中没有" | 企业场景里瞎答比不答更危险 |
| **误拒率 / 含糊率** | 有答案却拒答 / 给了倾向但没结论 | 拒答过头、和稀泥都是不可用 |
| **延迟 P50/P95** | 端到端毫秒 | 内网问答的可用性底线 |

用例分五类：**事实型 / 条款型 / 跨条款多跳 / 无答案（负例）/ 时效与版本**。
负例和时效类是"边界"所在，也是这套体系真正花时间的地方。

```bash
# 离线基线（不联网，改一条分块规则就能立刻复算）
python -m pipeline.ingest --dir data/sample --dry-run --dump chunks.jsonl
python -m eval.offline --chunks chunks.jsonl --cases eval/cases/enterprise_policy.json \
  --summary-json eval/report/offline.json

# 回归门禁：和仓库里的基线比，退化就是退出码 1
python -m eval.regression --baseline eval/baseline/offline.json --current eval/report/offline.json

# badcase 回流：线上"答得不对" → 候选用例 → 人工复核 → 并入用例集
python -m eval.badcase --feedback feedback.jsonl --cases eval/cases/enterprise_policy.json
python -m eval.badcase --feedback feedback.jsonl --cases eval/cases/enterprise_policy.json --merge

# 线上召回评测 / 端到端评测（给出策略清单时，按角色过滤召回块）
python -m eval.runner --cases eval/cases/enterprise_policy.json --kb-id <id>
python -m eval.runner --cases ... --kb-id <id> --policies data/policies.json --clearance 内部
python -m eval.runner --cases ... --kb-id <id> --mode chat --session-id <sid>
```

## 五、异常治理

线上出问题，八成不是模型不行，而是这几类（[`docs/04-接口联调与异常治理.md`](docs/04-接口联调与异常治理.md)）：

- **解析失败 / 入库积压**：`ops/doctor.py` 查待处理知识条数与最近失败原因；
- **空召回**：同一问题换关键词仍空 → 大概率是分块或清洗把内容洗掉了；
- **超时与重试风暴**：客户端做退避 + 抖动 + 熔断，避免故障时把上游打穿；
- **重复入库**：内容指纹去重，重跑流水线不会产生重复知识条目；
- **上传静默损坏**：带文件的重试必须重新打开句柄，否则第二次传的是空文件（已修 + 有单测）；
- **这批到底传成功没有**：批次台账 + `python -m pipeline.ledger --require-ok`。

```bash
python -m ops.doctor --base-url http://127.0.0.1:8080 --kb-id <id> \
  --ledger data/ledger/ingest.jsonl
```

## 六、从"能答"到"敢用"：九条业务边界

POC 能答对问题，不等于企业敢上线。真正拦着上线的问题，本仓库各落了一条：

| 业务问题 | 本仓库的做法 | 命令 |
|---|---|---|
| 答错了能追到哪？ | 稳定块 uid + 批次台账（源文件 sha256、块列表、状态） | `pipeline.ledger` |
| 老版本还在答？ | 按文件编号归组，只入库当前有效版本，`--as-of` 支持历史时点 | `pipeline.versioning` |
| 涉密内容会不会答给不该看的人？ | 文档级密级 + 库级准入 + 策略清单 + 检索后过滤，越权话术与无答案一致 | `pipeline.security` |
| 改完是变好还是变坏？ | 召回/要点双层指标 + 回归门禁（退化退出码 1） | `eval.regression` |
| 用例集怎么长大？ | badcase 回流：机器整理去重、人补考点、`--merge` 入集 | `eval.badcase` |
| 数值题算不准？ | 表格结构化 + 区间查询（"8 天及以上"能匹配 9 天） | `pipeline.tables` |
| 答到了但没解决？ | 业务指标：首次解决率/转人工/重复提问/单次成本，可分部门、列高频问题 | `ops.insights` |
| 文档改短后旧块还在？ | 按台账 uid 差异生成清理计划（变更/删除块清单） | `pipeline.prune` |
| 发布前到底查什么？ | 一条命令跑完体检/版本/密级/指标四道闸 | `ops.release` |

完整的业务分析与路线图（拒答分级、业务指标、成本账、组织分工、三阶段退出门槛）写在
[`docs/07-业务优化与演进路线.md`](docs/07-业务优化与演进路线.md)。

## 七、快速开始

```bash
pip install -r requirements.txt

# 1) 起 WeKnora（需要 Docker；模型与向量库地址按内网改 .env）
cd deploy && cp .env.example .env && docker compose -f docker-compose.zhitu.yml up -d

# 2) 清洗 + 分块 + 体检（不联网，先看切出来的块对不对）
python -m pipeline.ingest --dir data/sample --dry-run

# 3) 入库前体检 / 版本体检（可选，但建议放进流程）
python -m pipeline.quality --dir data/sample --min-score 60
python -m pipeline.versioning --dir data/sample

# 4) 入库（默认上传清洗稿，保证线上与评测同源；同时落策略清单与表格结构）
python -m pipeline.ingest --dir data/sample --kb-id <知识库ID> \
  --policies-out data/policies.json --tables-out data/tables.json

#    需要条款级引用时按块上传；docx/pdf 原样交给上游解析器需显式放行
python -m pipeline.ingest --dir data/sample --kb-id <ID> --mode chunks
python -m pipeline.ingest --dir data/sample --kb-id <ID> --allow-passthrough

#    （可选）文档改短/删条后，按台账差异生成旧块清理计划；只出清单，不自动删
python -m pipeline.prune --dir data/sample --ledger data/ledger/ingest.jsonl

# 5) 数值题旁路：区间查询，不让模型读表硬算
python -m pipeline.tables --dir data/sample --query "出差天数=9"
python -m pipeline.tables --dir data/sample --json data/tables.json --csv data/tables.csv

# 6) 评测 + 门禁（--policies 时按角色过滤召回块）
python -m eval.runner --cases eval/cases/enterprise_policy.json --kb-id <知识库ID> \
  --policies data/policies.json --clearance 内部
python -m eval.regression --baseline eval/baseline/offline.json --current eval/report/offline.json

# 7) 业务指标（用仓库里的样例日志试跑；可分部门、列高频问题、导出 badcase）
python -m ops.insights --log data/interactions.example.jsonl \
  --price-in 0.002 --price-out 0.008 --monthly-volume 10000 \
  --by-department --top 5

# 8) 发布自检：体检 + 版本 + 密级 + 指标，一条命令出结论（可进 CI）
python -m ops.release --dir data/sample

# 9) 线上自检（含批次台账）
python -m ops.doctor --base-url http://127.0.0.1:8080 --kb-id <知识库ID> \
  --ledger data/ledger/ingest.jsonl
```

没有 WeKnora 环境时，第 2、3、5、8 步和单元测试可以完整跑通：

```bash
pytest -q
```

## 八、目录

```
client/      WeKnora API 客户端（重试/限流/熔断/错误分类/响应适配/上传完整性）
pipeline/    清洗 → 分块 → 体检 → 版本/密级准入 → 表格结构化 → 入库 → 批次台账 → 旧块清理计划
eval/        评测用例、指标（召回+要点）、离线基线、badcase 回流、回归门禁
ops/         上线自检（doctor）、发布自检（release）、业务指标（insights）与排查
deploy/      docker compose 与内网配置
docs/        场景与范围、清洗分块、评测体系、异常治理、排查手册、交付基线、业务优化路线
data/sample/ 脱敏样例文档
tests/       单元测试（含 mock 服务端，离线可跑）
.github/     CI：单测 + 离线发布自检
```

## 九、已知边界

- 评测用例是**脱敏等价问法**，不能直接代表企业真实问题的分布，指标用于回归对比而非对外承诺；
- 分块规则针对制度类中文文档调过，产品手册（大量图文混排）需要另配规则；
- 表格只做"整表入块"，没有做表结构抽取，跨表数值计算类问题仍会答错；
- 版本/时效依赖正文里的文件编号与生效日期，未接文档管理系统；填错就判错；
- 密级只做到**文档级**，且默认从元信息/页眉抽取；docx/pdf 本地读不出密级，需 `--allow-passthrough`
  并自行在 WeKnora 侧设权限；
- 权限过滤只在 search 评测链路生效；chat 模式的上下文由问答服务拼装，需在那里套同一份策略清单；
- badcase 回流不自动判定对错：正例必须人工补 `must_include`，否则拒绝入集；
- 表格结构化只处理 Markdown 表（docx/pdf 需先由上游转 Markdown），合并单元格/多行表头不支持；
  查询只做等值/区间/包含，不做聚合与跨表 join——那些应该进数据库；
- 业务指标是**代理口径**：首次解决率靠"有用+未转人工+未重复"近似，没有接工单系统；
  评分覆盖率低时该指标不可信，报告里会直接标出来；
- 旧块清理只出**计划**（`pipeline.prune`），删除仍需在 WeKnora 管理端/API 执行；
  raw/cleaned 模式的引用标题由上游解析器决定，该模式下清单只能参考、不能直接当删除指令；
- 回归门禁目前只卡离线召回基线，chat 模式因成本原因按发版频率跑。
