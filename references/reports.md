# 执行报告与开发定位

在准备CLI命令、登记执行回执和验收报告时读取。本文件补充[contracts.md](contracts.md)中的机器契约。

## 每次命令独立保存

run可以包含多次CLI执行。每次分配新的`bundle_id`，重跑、认证重试及不同subset均保留独立目录：

```text
test/.cache/runs/<run-id>/
  attempts/<bundle-id>/
    results/result.json
    results/output/          # runner输出、截图、trace、error-context
    report/playwright/      # 同一次命令的HTML
    blob/                   # 同一次命令的原生blob ZIP
    command.json            # 脱敏命令、环境、时间、退出码
  summary.json
  report.md
  report.html               # 最终交付入口，未通过优先
  merged/playwright/        # 本轮全部blob合并的原生HTML及附件
```

选择执行范围前先完成[覆盖缺口门禁](workflow.md#覆盖缺口门禁与模块补全)。发现全部现有test只证明现有代码清单完整；必须与需求及计划比对，缺失场景默认按模块探索并补代码。最终全量CLI在可完成的补全结束后执行，只有用户明确选择本轮跳过的范围可以仅回归；未审批或不可执行的目标分别列受阻。

执行writer在运行前配置JSON、HTML、blob与runner输出目录。Windows用`py -3`，相对证据路径用`/`，PowerShell用`$env:变量名 = "值"`。沿用项目注入机制；Playwright可使用`PLAYWRIGHT_JSON_OUTPUT_FILE`、`PLAYWRIGHT_HTML_OUTPUT_DIR`、`PLAYWRIGHT_BLOB_OUTPUT_DIR`和`PLAYWRIGHT_HTML_OPEN=never`配合`--reporter=list,json,html,blob`、`--output`，或在项目配置显式设置各reporter路径，参数以安装版本为准。路径落在本次attempt目录；保留真实退出码，测试失败也收集和清理。

新run固定`native_merge_required=true`，每次CLI必须保留原始JSON和至少一个原生blob ZIP，正常同时生成同次HTML；最终必须核验全轮原生合并。仅旧run兼容JSON-only（html_report:null、html_origin:none），明确受限，不能用于新执行或推断默认目录HTML。HTML格式不受支持也不构成省略新run证据的许可。

开发验证跑受影响项，收尾默认全量现有自动化。首次回归前prepare-regression、配额与冲突规则见[最小命令顺序](workflow.md#最小命令顺序)；拆命令隔离输出/认证，分别领取配额。将真实发现与完整状态的regression_inventory核对全目标，未审批/缺依赖/权限不足逐项受阻，不静默过滤；默认简报无需展开全清单。

稳定用例可在其他流程探索编码期间单独回归，不等待全部探索完成；选择条件及读写隔离见[工作流](workflow.md#稳定回归与探索重叠)。首次实际执行前完成相关目标的审批/映射及fixture安全检查，避免跑完才发现无法绑定或需补资源账本。早期CLI与最终全量CLI分别留attempt；后续CLI复用清理预检，新增模块只补其范围。清理异常留一次诊断，输入未变不重试、改脚本或现场多轮安装；相关清理未完成不测试，独立范围继续。

离线重建HTML时使用`html_origin:rebuilt`，写明原始JSON及重建方式、时间和版本限制，说明没有重新执行测试。重建不能补造原JSON没有的步骤、截图或断言，不能替代同一次实际执行的原始HTML。

## 绑定与校验

执行者准备一个`report_bundle`事件，由主owner收集：

| bundle字段 | 必填性 | 值/门禁 |
| --- | --- | --- |
| bundle_id、run_id | 必填 | 本轮唯一CLI标识、当前run；实际重跑换bundle_id |
| command、cwd、environment、scope | 必填 | 真实脱敏参数数组、工作目录、授权环境、run内模块数组；不由脚本代跑命令 |
| setup_projects | 必填 | 实际Setup project名称数组，无则[]；不按标题猜Setup |
| started_at、ended_at、exit_code | 必填 | 带时区ISO8601、真实退出码，end≥start；JSON计时须落在调用窗口，exit0不得与runner失败冲突 |
| execution_result | 必填 | 同次CLI原始JSON，须在本次attempt目录 |
| blob_reports | 新run必填 | 非空、无重复的实际ZIP路径数组；旧run缺省[]仅受限兼容 |
| html_origin、html_report | 正常新执行提供 | same_execution与同次HTML路径；旧run无HTML用none/null，不作为新执行路径 |
| rebuild_source、rebuild_note | rebuilt条件 | 指向原execution_result并说明方式、时间/版本限制；离线重建不等于重跑，也不能代替缺失原始证据 |

可用`workflow.py schema --type report_bundle`查看实际形状，无需--project；该命令不是验证器，运行时仍检查全部门禁。以下时间、ID和路径为字段示例，必须替换为实际输入：

```json
{
  "type": "report_bundle",
  "bundle": {
    "bundle_id": "cli-01",
    "run_id": "<run-id>",
    "command": ["pnpm", "exec", "playwright", "test", "--reporter=list,json,html,blob"],
    "cwd": ".",
    "environment": "已授权环境标识",
    "scope": ["module-id"],
    "setup_projects": ["auth"],
    "started_at": "2026-10-26T01:00:00+00:00",
    "ended_at": "2026-10-26T01:02:00+00:00",
    "exit_code": 1,
    "execution_result": "test/.cache/runs/<run-id>/attempts/cli-01/results/result.json",
    "html_report": "test/.cache/runs/<run-id>/attempts/cli-01/report/playwright/index.html",
    "html_origin": "same_execution",
    "blob_reports": ["test/.cache/runs/<run-id>/attempts/cli-01/blob/<actual-name>.zip"]
  }
}
```

ZIP和附件一起保存摘要。旧bundle缺少blob仍可读取并标限制，不代表原生合并完成，也不允许新run省略blob。

默认只调用一次：

```sh
python3 <skill>/scripts/workflow.py collect-results --project PATH --run RUN --owner OWNER --input bundle-event.json
```

该命令在同一项目锁中登记并验证bundle，从真实JSON生成Setup、业务、retry及有依赖证据的notrun回执，经原有来源/审批/恢复门禁后一次原子提交state。失败不留下半登记bundle；原文件不删除。重复收集同一未变bundle不重复绑定；实际重跑使用新bundle_id。

默认简报包含数量、ID、原因及`collection.complete/unresolved`，不扫描历史run，global_residuals_checked=false；需要完整清单时用--full，--brief可显式指定。exit0只表示本次收集成功，不代表测试通过、清理完成或映射齐全；歧义保留待核实，不猜case。补足映射/依赖证据后重新收集，或record经核实事件。收集不自动批准/验收；全部命令收集→原生合并/登记→证据审阅和验收→report严格重核→release，最终安全检查不受简报影响。

每次CLI结束即收集，审批不足等门禁错误也立即保留原始结果并记录限制，不推迟到最后集中补账。登记错误由证据和映射修复，不靠重跑测试消除。相同输入的确定性错误完成一次定位后，修复或明确阻塞；结束时保留failed/partial及原始JSON索引，不能改写成checked。当前证据审阅、资源核对和明确限制完成即可按实际终态交付，历史绑定维护另行处理。

需要只读候选预览或诊断时，才使用原来的`record report_bundle → import_results.py → record events`分步接口，不与默认路径重复执行。生成的execution事件额外携带：

```json
{
  "bundle_id": "cli-01",
  "runner_test_pointer": "/suites/0/suites/0/specs/0/tests/0",
  "runner_result_index": 0
}
```

`runner_test_pointer`定位原JSON中的`tests/N`对象，`runner_result_index`定位该test的`results/N`。重复测试的每个test对象分别映射；retry的每个result分别映射。没有result且原test为skipped时索引为null。全部Setup、业务测试及未执行目标均登记，不能只收录最终通过或失败项。若发现项目测试标题与coverage不一致，先核实映射；保留原始结果，不把回执映射到其他用例。

`report_artifacts.py`由`workflow.py`加载，登记时检查：

- JSON、HTML和文件附件位于本次attempt目录；inline附件由所在报告保存
- JSON统计与其test记录一致；HTML与JSON的数量、测试标题、文件、project、结果、时间及逐次尝试一致
- 声明的附件实际存在；登记时保存文件SHA-256
- 执行回执与原JSON指针、结果、Setup归属及审核用例标题对应；同一原始result只登记一次

`validate --run`及`report`重核文件指纹与逐次映射，失败exit2并保留原因。HTML只读取已识别的Playwright内嵌ZIP格式；未知格式不可校验，不默认通过。保留原HTML、JSON和blob，记录阻塞并按安装版本处理适配；新run不能改用JSON-only绕过。旧run仅可按其既有历史限制供人工查看。

`summary.json`及`report.md`包含`report_bundles`和`report_consistency`。bundle中的`passed`表示已比较HTML/JSON，`json_only`表示仅提供JSON；整轮`checked`表示已登记bundle及所有原始result均有对应回执，`partial`表示仍有未绑定旧回执，`unregistered`表示尚未登记bundle，`failed`表示发现差异。旧run仍可读取，旧回执不能据此被声称已验证报告一致性。机器检查不证明命令实际参数、角色指定、业务预期或人类可读性，主Agent仍需审阅原始命令与必要报告内容。

## 最终合并与未通过优先

每轮最终交付一个`report.html`入口，收纳本轮全部CLI，包含Setup、业务、retry、重复执行及未执行目标。顺序为失败→非预期/重跑后通过→跳过/未执行→普通通过；保留每次原始尝试及其批次，不能用最终pass隐藏首次失败。入口直接链接合并的Playwright原生HTML，并保留各次HTML、原始JSON及错误详情入口；未绑定的原始失败也显示，并标明报告一致性限制。

原生合并由主Agent执行本地项目同版本Playwright，属于离线产物处理，不重跑测试：

1. 将本轮所有已登记`blob_reports`复制到独立合并输入目录，用bundleID前缀避免同名覆盖，核对数量和SHA-256；不混入历史run。本轮失败CLI也纳入，新run缺blob必须阻塞，不以“限制”替代门禁
2. 运行项目包管理器对应的`playwright merge-reports --reporter=html <input-dir>`，将`PLAYWRIGHT_HTML_OUTPUT_DIR`设为`test/.cache/runs/<run-id>/merged/playwright`，`PLAYWRIGHT_HTML_OPEN=never`。PowerShell使用环境变量赋值或显式merge配置。保持安装版本和testDir一致，不在线升级依赖
3. 用record登记下列merged_report事件。校验器要求bundle_ids恰好覆盖本轮全部CLI，并比较原始JSON与合并HTML中的每次尝试、结果统计、错误详情及附件内容摘要。合并不能丢失失败、重跑、Setup或skip；若Playwright版本/合并行为无法保持这些证据则列限制，不声称已核验
4. 核对覆盖、审批、逐次结果和资源清理，记录实际验收终态，再调用一次最终report，生成含终态的`report.html`未通过优先索引、summary及Markdown；检查成功后释放租约。原生HTML内部可能按文件/用例组织；交付入口固定逐尝试排序，作为定位失败的第一入口。索引是衍生视图，只有原生合并成功且已核验才显示“合并报告已核验”

```json
{"type":"merged_report","report":{
  "html_report":"test/.cache/runs/<run-id>/merged/playwright/index.html",
  "bundle_ids":["cli-01","cli-02"],
  "command":["pnpm","exec","playwright","merge-reports","--reporter=html","<input-dir>"],
  "started_at":"2026-10-27T01:00:00Z","ended_at":"2026-10-27T01:00:01Z","exit_code":0
}}
```

尚未进入下一轮回收的旧轮次若缺blob，交付各原始HTML及失败优先索引并明确“无法原生合并”；不通过重跑或把JSON重建索引冒充历史原生报告。新增CLI后原合并失效，须重新合并、登记；合并输入、输出及附件指纹都重核。summary的report_paths给出review_html和merged_html；合并不改变逐case结论或runner原始计数。

脚本核验的是产物内容一致性，命令及退出码由执行者如实登记。主Agent审阅真实进程输出及输入清单后确认原生合并成功；ZIP元数据或登记的命令文本不单独证明进程实际执行。

## 历史报告与回收

当前轮首次失败、修复、重跑和清理证据保留至结束。下轮只回收符合[回收契约](contracts.md#运行历史回收)的相关模块旧产物，保护当前/活跃/恢复run、有效baseline/审批及未核清资源恢复证据；历史失败结论不改写。

已登记回收日志的终态历史报告显示report_consistency=recycled及原统计/原校验状态，不重验已删除原始result，不把历史失败改成pass；部分回收仍检查保留文件摘要。没有日志的意外缺失继续failed。已回收路径显示文字说明，不生成失效链接。当前run必须严格校验全部JSON/HTML、附件与逐结果绑定。新回归不继承旧cli-01绑定错误；不完整历史bundle列limitations，归属及模块范围可确定的旧报告仍回收，不能确定的局部保留。历史校验异常继续如实显示，不阻塞新轮。

验收同时引用timing阶段区间并集、调度账本登记峰值、source_cache_hits及retention文件/字节，并呈现计划/结果的diagnostics：候选、选中、受保护数量，保护来源/字段/原因/样例路径及按原因排除数量，完整字段见[回收契约](contracts.md#运行历史回收)。零回收须显示zero_reason，无历史、全部活跃或审批/资源恢复保护均可合法为0；只有可清候选去向无法解释才是cleanup_candidate_mismatch（exit2）。清理计时引用plan.cleanup_scan_seconds及apply结果timing.cleanup_scan_seconds、timing.cleanup_apply_seconds；应用耗时含复核、隔离和删除，不称纯unlink。replayed=true时展示首次已存计时，不重复累计。说明实际测量和规划估计，不承诺固定耗时或倍数。

耗时至少区分用户请求到最终交付的可观测会话墙钟、run创建到报告生成的timing.wall_seconds、实际runner调用时长；会话时间由宿主证据补充，脚本内run墙钟不能代替用户体感。并行命令按区间并集统计，串行时才相加。现有任务/日志能够支持时补充准备、来源读取、探索、编码、工具修复、协调/收尾及人等，并标明重叠；阶段首尾跨度明确标为区间，不能当作持续工作时间。unknown单列，不将夜间空档推断为模型、网络或宿主故障。计数、时间和绑定差异尽量在最终报告生成前一次核对，避免交付前重复手写统计脚本。

## 覆盖补全状态

最终人类报告同时说明需求覆盖与现有测试执行，按模块列出有效需求数、计划case数、已有有效独立自动化、本轮新增/维护、已探索待编码、未探索/缺失代码、审批或环境阻塞、明确非自动化处置及用户本轮跳过的范围和数量。需求、case和runner test使用各自分母，重试不增加唯一case数；本轮新增可作为有效覆盖的子集，阻塞原因可重叠，不能将所有列机械相加。

从coverage、真实发现和本轮task/case/human记录汇总到现有人类报告，不增加逐动作统计文件；脚本未提供的字段由主Agent按证据补充，未知记null。用户明确跳过时，主要报告入口旁及最终答复突出“指定范围本轮经用户选择仅回归”，列决定引用、模块和剩余缺口；本轮决定不自动继承。缺失仍存在时，即使全部现有test通过，也只称“现有自动化全部通过”，不能称需求全覆盖或全部验收完成。

## Setup失败与未执行

Setup单列统计。只有在确认project依赖关系或runner日志后，才能将其依赖跳过的业务目标记录为：

```json
{
  "result": "notrun",
  "runner_result": "skip",
  "blocked_by": "<失败Setup的execution_id>",
  "reason": "业务project依赖认证project，认证失败，业务步骤未开始"
}
```

原runner的skipped数量保留在bundle中；业务口径计为未执行/受阻，并显示阻塞源。显式业务skip保留skip及原因。其他未执行原因按实际证据登记，不能因为存在一个Setup失败便把所有skip归到该Setup。

`business_executions`保留登记回执数量；`business_actual_attempts`排除skip/notrun，`dependency_blocked`单列上述依赖阻塞数。首次失败、重跑、flaky和最终结果各自保留，不能用最终passed覆盖失败历史。唯一业务测试数、原runner目标数和实际attempts分别解释。

## 一份可用的人类报告

主Agent至少审阅一份开发能据以定位问题的报告。HTML包含足够错误、回读附件和清理记录时，可作为主要阅读入口，`report.md`仅需保留工作流摘要、统计口径、差异与链接。无需重复维护两份详细叙述。

HTML缺失或信息不足时，在执行回执中补充`failure_details`，由脚本写入`report.md`：

```json
{
  "failure_details": {
    "expected": "编辑保存后修改时间应晚于编辑前",
    "actual": "名称已保存，但两次回读的修改时间相同",
    "phase": "保存后的API回读断言",
    "classification": "needs_decision",
    "location": "test/src/module/case.spec.ts:121",
    "readback": {"before": "15:37:59", "after": "15:37:59"},
    "cleanup": "已按准确ID清理，并确认查询不到",
    "evidence": ["test/.cache/runs/<run-id>/attempts/cli-01/results/readback.json"]
  }
}
```

分类使用`product_candidate|script|environment|needs_decision|deferred`。具体Bug判断仍需诊断；相同读数可能涉及时间精度或测试时序，不能仅凭失败断言直接裁决产品缺陷。报告保留稳定caseID、标题、每次尝试、预期/实际、阶段、文件行号、脱敏回读、截图/trace/error-context引用、问题分类和清理结果。没有的证据标为缺失，不补造。历史回执缺少字段时，Markdown显示待补充；主Agent安排补足其中一份人类报告后再交付。

验收顺序：核对报告绑定→检查逐次映射与统计→阅读失败/阻塞详情→核对资源清理→选定主要阅读入口。新执行须完成绑定后验收；旧run补审时保留未绑定限制，不能把`unregistered/partial`当成报告已核验。
