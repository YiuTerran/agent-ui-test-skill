---
name: ego-lite-playwright-e2e
description: Check current product requirements, ask the user which automation gaps to implement, explore selected Web scenarios with ego-lite, and maintain traceable Playwright regression. Not for API-only tests, one-off page inspection, or unit tests.
---

# Ego-lite与Playwright E2E

把有效需求、真实页面观察和Playwright证据连起来。先盘点覆盖缺口，再由用户选择按模块新增、全部新增或本轮跳过新增；按选择补写自动化，运行已有合格测试。

## 何时使用

适用于需求→真实Web探索→独立Playwright用例→回归与验收的闭环。不用于纯API/单元测试、一次性页面查看或不涉及测试覆盖的产品修复；这些任务直接使用对应工具，不初始化本工作流。

## 工作节奏（SHOULD）

仅三类工作允许投入较长判断时间：需求澄清、ego-browser页面探索、测试代码编写。来源快检、状态流转、清理、配额、用例发现、结果绑定、报告生成都是快车道：主Agent直接执行确定性操作，不为其派子Agent、不制作重复交接文件、不逐条轮询状态或反复向用户汇报。

1. 优先用一次`preflight`完成接管/续租、本地诊断和已提供来源观察/测试发现的差集汇总；已有run用`--run`，恢复用`--resume`。没有外部观察或发现清单时输出待检查项，按需补齐，不把本地文件存在当作来源新鲜或真实test发现。来源未变且快照完整时复用，不重读正文。
2. 列出尚未完成自动化的总数、各模块数量和case/缺口原因，征询按模块新增、全部新增或本轮跳过新增；按模块选择时确认具体模块，记录答复原话、清单与范围。预期澄清与新增范围选择分别处理。
3. 对获选缺口执行ego-browser探索→独立Playwright测试→受影响验证，同一探索者完成该场景代码；需求稳定、测试有效的case可并行回归。默认Agent上限4，浏览器探索与Playwright worker共享上限4，并按资源与冲突收紧。
4. 首次回归前只调用一次`prepare-regression`，用`attempt-init`生成固定产物目录和输出参数，为真实CLI领取配额。CLI结束且teardown完成后释放配额，`attempt-bundle`从真实回执和产物生成事件，立即`collect-results`登记绑定。受影响验证标affected；收尾全量标full并绑定当前完整发现清单，核对实际目标集合及所有shard。
5. 全部CLI完成后一次合并本轮Playwright HTML、将失败/重跑/未执行置前，一次`report`生成最终入口并释放配额和run。只在命令结果指出问题时做一次针对性诊断；相同输入不重复试错。

命令输出就是下一步输入。默认不单独调用`status`、`validate`、retention `plan/apply`或`import_results.py`；只有定位实际错误、手动预览/恢复或映射不明确时才使用。已有来源检查、发现清单、审批与runner证据直接引用，不重写成多份摘要。新增自动化范围、真实歧义、范围扩大或新增高风险写操作由用户决定。

| 状态 | 就绪依据 | 下一动作 |
| --- | --- | --- |
| 接管/盘点 | preflight的run、sources及coverage_diff | 补真实观察/完整发现，列模块缺口并取得新增范围选择 |
| 探索/编码 | 新增范围已获选择，来源、预期、身份及资源边界有效 | 同一探索者推进所选case与受影响验证 |
| 执行/绑定 | prepare-regression、Attempt输出参数及实际配额 | 真实CLI→teardown→release配额→attempt-bundle→collect-results |
| 收尾 | 全部CLI已绑定，资源核验完成 | 原生merge→record真实验收→report→release |

字段不确定时用`validate-input`在内存重放真实门禁；不把它作为每次提交的额外前置。`attempt-check`仅用于诊断，正常`attempt-bundle`已含检查。需要复盘时用只读`timing`，未知项保持null；传入宿主日志时按当前Agent供应商设置`session.host_format`（Claude Code为`claude_code`，Codex为`codex`），不确定时用`auto`。命令输入见[本地辅助入口](references/local-tools.md)。

首个获准新增的模块就绪即探索/编码。连续3次冗余、非业务本地调用均未推进任何已就绪工作时，停止重复协调，直接推进独立任务或针对已知阻塞处理；必要的初始化、来源核验、安全清理及有效诊断不计入。无需为此增加汇报或计时墙，外部等待和实际runner时间单列。

## 安全门禁（MUST）

MUST是不可省略的证据/安全约束；SHOULD是默认效率路径，有实际依赖或恢复需要时可调整，不弱化门禁。

- 本轮全量核对需求→case→实际独立test；新增场景的探索、测试及所需fixture以用户对缺口清单的明确选择为前置。普通“开始测试/执行全部测试”只授权盘点及已有回归；沉默、超时和历史用例审核均不构成新增授权。待选择时可继续只读盘点及已有回归，清单外新缺口再次确认，同轮清单内的有效授权直接复用。
- 用户选择跳过或仅新增部分模块时，保留剩余缺口及决定证据，报告区分本轮新增范围、现有自动化执行与需求覆盖。
- 需求决定预期，浏览器说明实际行为。业务差异保留证据；业务代码修复需另有授权。
- 既有业务数据只读。专用E2E资源按intent→准确ID/owned证明→逆依赖清理→absence check处理；未知创建结果不得重试或猜测删除。
- 独立case并行、共享资源按账号/组织/配置/数据冲突组局部串行。执行前领取共享配额，teardown后释放；资源信息不足从一个浏览器任务开始。
- 新run的`native_merge_required=true`：每个bundle必须绑定当前schema的Attempt计划及摘要，每次CLI必须有原始JSON和blob，正常同时生成HTML；完整绑定Setup、业务case、retry及未执行结果，最终合并全部blob并核验。JSON-only保留为旧run的受限兼容路径。失败、重跑后通过及未执行置前，不以缺失证据推断通过。
- 新Attempt仅允许追加固定reporter/output参数，执行选择条件进入计划command。新run的完整验收必须满足`regression_completeness=complete`，覆盖绑定完整发现清单的收尾full及全部目标/分片；业务结果另行核验。缺少计划、full或目标时阻止完整验收，`not_declared`仅用于旧run；完整发现声明仍需真实命令审阅。
- 每轮执行前回收相关模块旧报告；只自动删除缓存白名单内安全归属明确的产物，保护当前run、有效审批/baseline及未核清资源恢复证据。回收记录不能改写历史结论。
- 明确区分覆盖与执行、UI与API、模拟与真机、local与CI。未知计时记`null`，不把等待空档归因为测试时间。

## 按需参考

- 输入准备、恢复与覆盖补全：[工作流](references/workflow.md)
- 事件字段、CLI、平台和缓存回收契约：[契约](references/contracts.md)
- Playwright输出、绑定、报告合并：[报告绑定](references/reports.md)
- 实际ego-lite、认证和人工等待：[浏览器与认证](references/browser-and-auth.md)及当前安装的ego-browser技能
- 统一预检、事件预校验、Attempt模板和耗时：[本地辅助入口](references/local-tools.md)

内部工具按需调用，字段见[契约](references/contracts.md#提速辅助脚本)：
- `scripts/scheduling.py acquire|release`：实际探索/CLI前领取共享配额，teardown后释放
- `scripts/source_cache.py capture|verify`：新来源导出后留指纹清单，后续有新观察时校验复用
- `scripts/retention.py plan|apply`：手动预览/恢复；日常回归已由prepare-regression组合调用

脚本仅处理本地状态、配额、来源缓存、安全回收和报告核验，不启动Agent或执行产品测试，不读取远端来源。入口为`python3 <skill>/scripts/workflow.py`（Windows用`py -3`）。`init`只补缺；`doctor`只读诊断；`schema`查看输入字段。`record`/`collect-results`默认简报，`--full`按需展开；最终验收/报告仍完整核验。

项目路径：配置`test/spec.md`、计划/coverage在`test/plans/`、用例在`test/src/`、临时状态和大产物在Git忽略的`test/.cache/`。

## 最小命令顺序

`WF/SCHED`分别为技能scripts/workflow.py、scripts/scheduling.py路径，`PROJECT/OWNER`取真实项目和主任务标识，`RUN`取start返回值。`SOURCE_EVENTS/BUNDLE_EVENT/FINAL_EVENTS`按真实来源/审批、CLI产物、合并/验收写出，init不创建。`QUOTA_REQUEST`按[配额契约](references/contracts.md#双预算与就绪任务)声明实际读写集/workers；`QUOTA_RESULT`保存在忽略的缓存内，`RELEASE_REQUEST`取该结果lease中的lease_id/release_token。其余输入见[工作流](references/workflow.md#输入准备与命令边界)。每步成功再继续，收集成功不等于测试通过。

```sh
python3 "$WF" init --project "$PROJECT"
# 确认spec/coverage和Git忽略规则，再接管
python3 "$WF" preflight --project "$PROJECT" --owner "$OWNER"
# 设置RUN；真实来源读取/审批后写SOURCE_EVENTS，恢复时另含recovery_review
python3 "$WF" record --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$SOURCE_EVENTS"
# 有缺口时列模块清单并取得用户选择，将automation_expansion事件写入SCOPE_DECISION
python3 "$WF" record --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$SCOPE_DECISION"
# 仅对获准的case完成探索/代码/fixture及case更新；无缺口时省略上面的选择步骤
python3 "$WF" prepare-regression --project "$PROJECT" --run "$RUN" --owner "$OWNER"
# ATTEMPT_INPUT含真实command/cwd/environment/scope/setup_projects/regression_mode；全量填full并绑定当前发现清单
python3 "$WF" attempt-init --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle "$BUNDLE" --input "$ATTEMPT_INPUT"
python3 "$SCHED" acquire --project "$PROJECT" --input "$QUOTA_REQUEST" > "$QUOTA_RESULT"
# 核实acquired=true，应用attempt-init输出参数，实际运行CLI；即使失败也teardown
# 保存实际argv/起止/退出码到该attempt的command.json；从领取结果提取RELEASE_REQUEST
python3 "$SCHED" release --project "$PROJECT" --input "$RELEASE_REQUEST"
python3 "$WF" attempt-bundle --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle "$BUNDLE"
# BUNDLE_EVENT取attempt-bundle返回的input路径
python3 "$WF" collect-results --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$BUNDLE_EVENT"
# 每CLI分别收集；全部完成后实际merge，写含merged_report及真实acceptance的FINAL_EVENTS
python3 "$WF" record --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$FINAL_EVENTS"
python3 "$WF" report --project "$PROJECT" --run "$RUN" --owner "$OWNER"
python3 "$WF" release --project "$PROJECT" --run "$RUN" --owner "$OWNER"
```
