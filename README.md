# 私人长文本翻译

本地文件优先的 EPUB / Markdown 翻译 CLI，第一版语言对为 English → 简体中文（`en` → `zh-Hans`）。源文件、翻译状态、术语表、QA 记录和输出都保存在独立的 `projects/<id>/` 工作区；每个项目自带 Git 忽略规则，在 Git 仓库内初始化时还会确认私有状态确实被忽略。

翻译与复核文本会发送到用户配置的 provider。内置适配器兼容 OpenAI Chat Completions API；项目不会把文件发送到其他服务，也不是纯离线工具。API 密钥只从 `OPENAI_API_KEY` 环境变量读取，不写入项目。

## 安装与运行

需要 Python 3.11+、Pandoc 3+（Markdown AST）和 lxml。EPUB 总是执行内置 ZIP、package、spine、资源、链接、片段 ID 与 XML 结构验证；如果系统 PATH 中有 `epubcheck`，还会追加运行它。

```powershell
python -m pip install -e .
$env:OPENAI_API_KEY = "..."
$env:PRIVATE_TRANSLATE_MODEL = "your-model"
# 可选：兼容 API 的其他地址
$env:PRIVATE_TRANSLATE_BASE_URL = "https://api.openai.com/v1"

$project = translate init .\book.epub
translate run $project --bilingual
translate status $project
```

`init` 也支持 `.md`。默认只产出目标语；`--bilingual` 会同时产出目标语版和双语版，并将这个选择保存在项目中。输出名稳定为：

```text
book.zh-Hans.epub
book.en-zh-Hans.bilingual.epub
book.zh-Hans.md
book.en-zh-Hans.bilingual.md
```

`run` 与 `resume` 可重复执行；未变化且已完成的单元会跳过。provider 暂时失败时，已保存的章节状态保留。章节修订后会进行新一轮完整章节复核；之后进行全书术语/一致性复核。

## 抽检与最终验证

`run` 生成的是待抽检候选输出。最终构建和 `validate` 需要当前运行的两轮连续抽检证据：

```powershell
$sample = translate spotcheck $project --seed 17
Copy-Item "$sample\review-template.json" "$sample\reviews.json"
# 两位独立审阅者分别填写自己的 reviewer_id、assigned unit scores、style_debt_count 和 findings。
# 完成本轮并满足条件后，以新 seed 创建第二轮并填写其 reviews.json。
$sample = translate spotcheck $project --seed 18
Copy-Item "$sample\review-template.json" "$sample\reviews.json"
translate validate $project
translate build $project
```

每位审阅者的 `scores` 必须覆盖 `review-template.json` 中分配给其 slot 的每个单元，分数为 0–100；每人平均分至少 92、最低单项至少 88，style debt 必须为 0，且 reviewer ID 不同。`findings` 没问题时填空数组；发现问题时填写 `unit_id`、`family_id`、`description`。每一轮使用不同 seed，默认抽检计划最多四轮，当前运行最近两轮都通过才可最终验证。

发现缺陷族时，在该轮目录写入 `closure.json`。它必须记录 `family_id`、缺陷定义、全书审计方法、整个声明分层的每个单元审计结果（`clear` / `fixed` / `exception`），并给出每个修复的前后 target hash 与修复说明，或为合理例外给出理由。完成修复后重新运行 `translate run` 以执行章节和全书复核，再以新 seed 运行 `translate spotcheck`。只修抽中的单个样本不能关闭缺陷族。

如源文件变化，先运行 `translate resume $project` 重新解析/翻译并完成 QA；随后用 `translate spotcheck $project --new-run` 创建新的当前抽检运行。需要仅重启抽检时也可使用 `--new-run`；旧证据仍保留在 QA 目录中。

`run` 候选输出、抽样清单、`reviews.json` 与 `closure.json` 都位于被忽略的本地项目工作区。审阅者身份和分数由用户/审阅者填写，验证器只校验所提供的证据与当前项目状态的一致性。

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

首次 `run` 会在翻译前发现重复专名和术语，并通过配置的 provider 生成 `glossary/candidates.csv` 提案。人工将条目的 `status` 改为 `approved` 后，下次运行会把它锁入 `terms.csv`；只有 `approved` 项进入强制术语表。`proposed` 与 `rejected` 项不会影响翻译。OpenAI-compatible provider 默认按 8k context / 2k output 的保守能力分批，可用 `PRIVATE_TRANSLATE_MAX_CONTEXT_TOKENS` 和 `PRIVATE_TRANSLATE_MAX_OUTPUT_TOKENS` 声明当前 endpoint 的能力。

第一版限制：只提供 `en` → `zh-Hans`；不做 OCR；不翻译图片像素、代码、公式语法、URL、CSS 或 JavaScript。支持 EPUB 3 导航与 EPUB 2 NCX；无法读取的加密 EPUB / DRM 会明确拒绝；符合 EPUB 规范的字体混淆项只原样保留，不解密。EPUB XHTML 以原 DOM 为基础改写翻译文本，并保留原 CSS 与其他 ZIP 资源；双语版按块插入目标语。Markdown 依赖 Pandoc AST 并启用 Markdown header attributes，以保留显式标题锚点。当前机器未检测到 `epubcheck` 可执行文件，因此只可报告内置结构验证结果，不能声称 EPUBCheck 已通过。
