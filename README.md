# 私人长文本翻译

**简体中文** | [English](README_en.md)

本地文件优先的 EPUB / Markdown 翻译项目，正式执行入口为 **Codex / host integration → Python core**，第一版语言对为 English → 简体中文（`en` → `zh-Hans`）。源文件、翻译状态、术语表、QA 记录和输出都保存在独立的 `projects/<id>/` 工作区；每个项目自带 Git 忽略规则，在 Git 仓库内初始化时还会确认私有状态确实被忽略。

正式宿主流程中的术语提案、翻译、章节复核、全书一致性及修复均由 Codex Agent 执行。Python core 不构造或调用 HTTP provider，不需要配置独立的 provider API 密钥；模型请求由宿主接管。内部 API 执行路径继续使用既有 provider adapter，其密钥只从环境变量读取，不写入项目。

## 安装与运行

需要 Python 3.11+、Pandoc 3+（Markdown AST）和 lxml。安装 Python 包：

```powershell
python -m pip install -e .
```

包不注册产品终端命令。Python 包、core API 与 `python -m unittest` 测试入口保留。EPUB 总是执行内置 ZIP、package、spine、资源、链接、片段 ID 与 XML 结构验证；如果系统 PATH 中有 `epubcheck`，还会追加运行它。

本地验证使用 `python -m unittest discover -s tests -v`，保留 137 单元的小规模 long-book smoke；50,100 单元大书回归会自动跳过，仅在 GitHub Actions 中执行。现有 `large-book-regression` 任务在相关核心代码或大书测试变更 push 到 `main` 后运行，也支持 `workflow_dispatch` 手动触发。

代码 commit、push 后，若需要大书回归，确认 GitHub 已开始执行大书测试，并等待其他测试任务完成，即可停止等待、报告各任务当前状态及运行链接。大书测试继续在 GitHub 上运行；运行中不算通过。

### Codex / host integration

宿主通过 `private_translate.host` 直接驱动 core。Python 在需要 Agent 时返回待执行任务，不等待模型或宿主回调；宿主执行任务并提交 JSON 后，再次调用 core 继续。正常 core 异常直接抛出，由宿主呈现错误。

仓库的 `AGENTS.md` 已包含 Codex 调度步骤：用真实的 `spawn_agent` / `followup_task` worker 执行返回任务，并将逻辑身份绑定到实际 worker。现有 `gpt-6-luna-max` 选择对应 `luna_worker` 的 `gpt-6-luna` + `max`；传入 host API 的模型值为实际模型名。

```python
from pathlib import Path

from private_translate.core.project import create_project
from private_translate.core.pipeline import build_project, validate_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.host import (
    advance_host_run, get_host_request, get_project_status, submit_host_result,
    start_host_spotcheck, finish_host_spotcheck,
)

capabilities = ProviderCapabilities("codex-host", "gpt-6-luna",
    max_context_tokens=8000, max_output_tokens=2000,
    supports_json_mode=True, supports_parallel_requests=True)
# 这里声明本次任务预算；根据实际宿主能力调整。
project = create_project(Path("projects/inputs/book.epub"), Path("projects"))
status = get_project_status(project)
request = advance_host_run(project, capabilities=capabilities, bilingual=True,
                           agent_model="gpt-6-luna", review_model="gpt-6-luna")
# Python 调用在此结束；Codex 在调用之外调度 request 中的真实 worker。
```

`request["status"] == "awaiting_host"` 时，宿主读取 `tasks` 中的 `task`、`model` 和 `worker_id`，用真实的宿主 Agent 执行：术语任务按 `required_result_schema` 返回提案，翻译任务返回 TranslationPatch，章节复核返回 ReviewEvidence，全书一致性任务返回指定 schema 的问题证据。随后调用 `submit_host_result(project, result)` 存储经过 core 校验的结果，再调用 `advance_host_run`。返回 `spotcheck_pending` 时，候选输出已经构建，仍需要现有两轮抽检。

`start_host_spotcheck(project, seed=17, reviewer_a_id=worker_a_id, reviewer_b_id=worker_b_id, model=host_spotcheck_model)` 返回两个 SpotcheckTask。审阅者 ID 应绑定两个真实、独立的宿主 worker；宿主按任务指定的模型执行，并分别提交 SpotcheckEvidence。`finish_host_spotcheck` 在两份结果齐全后执行已有证据接收与评分流程。使用不同 seed 完成第二轮后，调用 `build_project` 和 `validate_project`。

`get_project_status(project)` 返回 `project_id`、实际运行 `status`、`unit_counts`、交接 `host_status` 和 `pending_task_ids`。刚初始化的工作区为 `imported`；查询不会解析输入或派发 Agent。翻译完成的单元以 `qa_pending`、`qa_passed` 等状态汇总，其余单元使用 translation status。

任务交接保存在 `work/host/request.json`，宿主结果保存在 `work/host/results/<task_id>.json`，都位于被忽略的项目目录。Python 进程退出后用 `get_host_request` 恢复待执行任务；已提交结果显示为 `results_ready`。相同结果重复提交只确认存储，不重复修改译文；不同结果不能覆盖已有结果。真正合并 patch、记录章节 QA 和抽检完成状态的是后续的 advance/finish 操作。

复核任务先保存 `pending` 生命周期，再保存可被恢复扫描发现的 task；已有证据的任务恢复时不重派。术语提案已写入、交接尚未确认时，恢复会核对完整候选行是否与原任务应用已保存提案后的状态一致，批准仍由人工完成。全书修复先在现有 patch archive 保存 `pending` 记录、修复数量和全书应用前后 target hash，再写 canonical 并标为 `applied`；中断后恢复只接受这两种明确状态，完成归档和计数后继续章节及全书重审。本任务候选行、批准术语或其他译文的人工变更仍会被拒绝。

章节 PASS 与任务证据只在来源、译文、审阅者、译者身份、模型、rubric 和完整批次上下文一致时复用；相关批准术语变化会使相应章节重新审阅。全书 PASS 同时核对批准术语与复核配置，待完成的全书轮次必须沿用原配置。新章节复核开始后 QA 保持 pending，完成零缺陷审阅后才恢复 passed。最终构建、验证与抽检创建都会核对全书报告已有的 glossary hash，批准、修改或撤销术语后需先完成必要修订与复核。内部 API 路径也比较其模型、规则和发送的上下文。

一次待完成运行保持相同的能力预算、模型和复核身份配置，同一项目的 core 写入操作由宿主顺序调度。提交前若来源变化，core 明确拒绝；正常翻译与复核修复恢复沿用原 TranslationTask ID。术语候选行在等待期间被人工修改，或全书一致性任务的译文/批准术语变化时，旧任务结果明确拒绝。宿主负责实际模型调用，Python 不新建模型客户端或在后台等待。

术语提案最后一批被 core 接收后，返回 `terminology_ready`，尚未创建正文翻译任务。宿主可让用户检查并批准候选词，然后再推进；未批准的提案仍不会锁入翻译。术语任务使用 `agent:terminology-owner`，可绑定现有译者 worker；一致性审阅者与译者保持独立。全书审阅按术语收集跨章节出现记录并分批，所有任务证据完整后才判定通过；发现问题交回原译者修复，经完整章节复核和新一轮全书审阅后再构建候选。

`create_project` 也支持 `.md`。默认只产出目标语；`advance_host_run(..., bilingual=True)` 会同时产出目标语版和双语版，并将这个选择保存在项目中。输出名稳定为：

```text
book.zh-Hans.epub
book.en-zh-Hans.bilingual.epub
book.zh-Hans.md
book.en-zh-Hans.bilingual.md
```

`advance_host_run` 可重复执行；未变化且已完成的单元会跳过。宿主任务暂时未完成时，已保存的任务和章节状态保留。章节修订后会进行新一轮完整章节复核，并让全书一致性与最终译文修订匹配。

## 抽检与最终验证

`advance_host_run` 生成的是待抽检候选输出。最终构建和验证需要当前运行的两轮连续抽检证据。每轮由宿主在 Python 调用之外执行两个真实 worker，并调用 `submit_host_result` 提交其 JSON：

```python
request = start_host_spotcheck(project, seed=17,
    reviewer_a_id=worker_a_id, reviewer_b_id=worker_b_id, model="gpt-6-luna")
# 宿主执行两份任务并提交 worker JSON 后：
round_result = finish_host_spotcheck(project)
# 用新 seed=18 重复上述交接；最近两轮通过后：
outputs = build_project(project)
validation = validate_project(project)
```

每位审阅者的 `scores` 必须覆盖其 SpotcheckTask 分配的每个单元，分数为 0–100；每人平均分至少 92、最低单项至少 88，style debt 必须为 0，且 reviewer ID 不同。`findings` 没问题时填空数组；发现问题时按任务 schema 填写 `unit_id`、`family_id`、`category`、`description` 和 `reusable_rule`。每一轮使用不同 seed，默认抽检计划最多四轮，当前运行最近两轮都通过才可最终验证。

发现缺陷族时，宿主使用既有 `core.execution.create_translation_task`、`apply_translation_patch` 接口驱动原译者修订，再调用 `advance_host_run` 执行章节和全书复核。宿主读取当前 canonical 文档，用 `core.spotcheck.close_spotcheck_defect_families` 提交闭环记录。记录必须覆盖缺陷定义、全书审计方法、整个声明分层的每个单元审计结果（`clear` / `fixed` / `exception`），以及每个修复的前后 target hash 和修复说明，或合理例外的理由。core 校验后保存 `closure.json`；随后用新 seed 开始下一轮。只修抽中的单个样本不能关闭缺陷族。

没有待完成交接时，`advance_host_run` 会重新解析来源并保留仍可匹配的状态。已排队任务的来源若变化，提交与推进会明确拒绝。重新解析/翻译并完成 QA 后，可用 `start_host_spotcheck(..., new_run=True)` 创建新的当前抽检运行；仅需重启抽检时也使用该参数，旧证据保留在 QA 目录中。

候选输出、抽样清单、worker evidence、`reviews.json` 与 `closure.json` 都位于被忽略的本地项目工作区。审阅者身份由宿主绑定，分数和发现由真实 worker 给出；验证器校验所提供证据与当前项目状态的一致性。

## 工作区

```text
projects/<id>/
├── project.json
├── source/       # 原始输入副本，永不覆盖
├── work/         # canonical 文档与断点状态
├── glossary/     # candidates.csv 提案、terms.csv 锁定术语、occurrences.json 本地索引
├── qa/           # 章节、全书、抽检、缺陷闭环、alignment
├── state/        # 最近运行状态
└── output/       # 目标语与可选双语候选/最终产物
```

首次推进会在翻译前本地发现重复专名和术语，并通过宿主 Agent 生成 `glossary/candidates.csv` 提案。人工将条目的 `status` 改为 `approved` 后，下次运行会把它锁入 `terms.csv`；只有 `approved` 项进入强制术语表。`proposed` 与 `rejected` 项不会影响翻译。正式宿主分批使用显式传入的 `ProviderCapabilities` 和现有语言包预算，不依赖 HTTP provider 客户端。

原生任务按完整 JSON（包括规则、ID、哈希、schema、术语和上下文）估算输入 token，并计入既有的请求开销，另预留配置的输出预算。正文翻译、章节复核、全书一致性和修复会继续细分批次；修复只携带本批相关问题。单个单元的完整任务仍超预算时明确报错，不派发超预算任务。估算沿用项目现有算法，实际模型的 token 数仍由宿主决定。已保存的待执行任务恢复时保留原 ID 和内容，不重新分批。

第一版限制：只提供 `en` → `zh-Hans`；不做 OCR；不翻译图片像素、代码、公式语法、URL、CSS 或 JavaScript。支持 EPUB 3 导航与 EPUB 2 NCX；无法读取的加密 EPUB / DRM 会明确拒绝；符合 EPUB 规范的字体混淆项只原样保留，不解密。EPUB XHTML 以原 DOM 为基础改写翻译文本，并保留原 CSS 与其他 ZIP 资源；双语版按块插入目标语。Markdown 依赖 Pandoc AST 并启用 Markdown header attributes，以保留显式标题锚点。当前机器未检测到 `epubcheck` 可执行文件，因此只可报告内置结构验证结果，不能声称 EPUBCheck 已通过。
