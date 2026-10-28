# 工作流：慢判断，快程序

先按[技能入口](../SKILL.md#先选路径)选择路径。真实E2E看[最小命令顺序](#最小命令顺序)，按报告修复看[快车道](#按报告修复快车道)；字段/安全细节查[契约](contracts.md)，执行/合并细节查[报告](reports.md)。MUST为安全/证据门禁；SHOULD为默认效率路径，必要的诊断和恢复可以调整顺序，但不跳门禁。

## 最小命令顺序

以下顺序仅用于真实E2E。已有回归复用有效输入，明确只回归时直接登记跳过新增；仅本地修复见[快车道](#按报告修复快车道)。旧start/renew路径继续可用，已有就绪输入不重复预检。实际CLI/merge不是workflow代跑。

`WF/SCHED`分别为技能scripts/workflow.py、scripts/scheduling.py路径，`PROJECT/OWNER`取真实项目和主任务标识，`RUN`取start返回值。`SOURCE_EVENTS/BUNDLE_EVENT/FINAL_EVENTS`按真实来源/审批、CLI产物、合并/验收写出，init不创建。`QUOTA_REQUEST`按[配额契约](contracts.md#双预算与就绪任务)声明实际读写集/workers；`QUOTA_RESULT`保存在忽略的缓存内，`RELEASE_REQUEST`取该结果lease中的lease_id/release_token。其余输入见[工作流](#输入准备与命令边界)。每步成功再继续，收集成功不等于测试通过。

```sh
python3 "$WF" init --project "$PROJECT"
# 确认spec/coverage和Git忽略规则，再接管
python3 "$WF" preflight --project "$PROJECT" --owner "$OWNER"
# 设置RUN；真实来源读取/审批后写SOURCE_EVENTS，恢复时另含recovery_review
python3 "$WF" record --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$SOURCE_EVENTS"
# 完整非空缺口清单就绪时，新增覆盖先确认范围，只回归则直接记录skip；无缺口省略此步
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

### 输入准备与命令边界

- **初始化**：init仅补spec/coverage，按真实需求确认配置、固定范围和映射；缓存写入前核实Git忽略。doctor只读诊断，不自动修复或批准。
- **SOURCE_EVENTS**：实际读取来源并保存快照，实际审批后保存决定/范围证据，再写单事件或数组JSON。恢复还需recovery_review；已有有效baseline未变时只登记source:unchanged，不重做批准。
- **新增范围**：新增覆盖时将模块缺口清单交给用户选择，用automation_expansion登记真实答复、完整非空清单和范围。明确只回归则保存用户原话与跳过新增决定，不重复征询；完整清单就绪才登记skip事件，无缺口不造事件。清单待核实不阻挡已有合格测试执行，也不以部分清单冒充完整清单或新增授权；完整回归收尾仍核对实际发现。
- **探索/代码**：在获准范围内补独立测试与真实认证/数据fixture，以case事件更新coverage和完成状态；待选择或未获选的缺口保持待补，已有合格测试继续回归。
- **实际CLI**：先prepare-regression、领取真实workers配额，再运行spec批准的项目命令。每次独立attempt目录，用JSON/HTML/blob reporters及输出目录参数（详见[报告](reports.md#每次命令独立保存)）；记录真实起止/退出码，失败也teardown、核对资源和释放配额。
- **BUNDLE_EVENT**：将该CLI真实JSON/HTML/ZIP路径、命令、环境、scope和Setup名称写为report_bundle，立即collect-results。多CLI换bundle_id分别收集；核实complete/unresolved，不为修复登记错误重跑测试。
- **FINAL_EVENTS**：全部CLI收集后，按[原生合并步骤](reports.md#最终合并与未通过优先)复制本轮全部ZIP、核对摘要并实际merge；成功后写merged_report与基于证据的acceptance数组，再report→release。新增CLI须重合并；无可执行目标只记阻塞，不造空bundle。

首次来源/审批事件形状示例（所有ID、版本、marker和路径须取真实值；示例文件不由init创建，也不代替审批）：

```json
[
  {"type":"source","source_id":"src","freshness_marker":"<start返回值>","status":"changed","observed_revision":"v1","business_change":true,"baseline_available":false,"evidence":["test/.cache/documents/source-v1.md"]},
  {"type":"task","task_id":"impact-src","phase":"impact","status":"complete","owner":"<OWNER>","source_id":"src","case_ids":["C1"],"affected_case_ids":["C1"],"evidence":["test/plans/module.md"]},
  {"type":"task","task_id":"review-src","phase":"case_review","status":"approved","owner":"<OWNER>","source_id":"src","case_ids":["C1"],"evidence":["test/.cache/documents/approval.md"]},
  {"type":"baseline","source_id":"src","approved":true,"revision":"v1","fingerprint":null,"baseline_path":"test/.cache/documents/source-v1.md"}
]
```

未批准则记录clarify/human等待，不能照抄approved；字段门禁见[契约](contracts.md#record事件)。`python3 "$WF" schema --type execution`或`--type report_bundle`可查看输入形状，无需项目参数，不执行验证。日常无需额外status，诊断完整清单用status --full，精简查看用--brief。

## 一次接管与快检

- 正常继续一次renew；中断一次start --resume并恢复核对；新轮start --new-round。新轮只承接未决义务，不继承旧执行/绑定错误。
- 来源版本/可靠内容指纹快检与本地test发现、需求-计划-coverage差集并行完成，一次批量登记。仅指纹匹配且快照完整时复用正文/附件；不可读记unknown，不猜未变。
- renew不刷新来源或重审身份/配置/报告；恢复、配置变化或输入失效才检查受影响项。命令输出直接作后续依据，不无目的重复status/相同文件读取。

## 按报告修复快车道

入口是已有失败报告或用户已批准的修复方案。沿用caseID、失败证据、已确认预期及本轮授权；证据缺失、需求变化或同一症状可能有不同根因时，只补相关项，不重开全项目需求盘点。

1. **一次定位**：批量读取目标仓库AGENTS、Git状态/已有diff、失败步骤与请求/回读、相关生产代码及测试/fixture。只追踪与失败有关的输入→校验→提交→持久化→回读链路，先区分产品、脚本、环境问题；找到足以解释失败的证据即可进入修改，不继续横向通读模块。已有有效复现不重复跑全量。
2. **尽早验证**：保留原有修改，按用户授权修复同根因代码，并先跑一个代表用例。本地组件或服务测试从现有同类fixture复制必要结构，先核对真实组件props、注入键、初始化请求和异步触发条件，再扩展边界断言。失败时按首个真实错误修正，不连续猜mock、不用空实现绕过关键交互。实现复杂度超出原判断时按实际证据调整方案，不以快车道为由省略定位。
3. **一次收尾**：代表用例稳定后跑受影响测试；共享fixture变化纳入其消费者。类型检查、构建和后端测试可按独立读写集批量启动，收集真实退出码。某项通过后，仅当其输入/依赖变化或需补真实证据时重跑；仅改文档不重跑业务构建。验证期间冻结其读取的代码，不边改边以旧结果交付。
4. **按范围交付**：本地修复无需init/preflight、远端资料刷新、浏览器探索、E2E配额或原生报告合并。交付改动、实际本地验证与未执行项；未部署则列部署后复测case，不把组件/服务测试当作线上通过。需要真实复测时先确认目标环境已包含修复，再走真实E2E门禁；定向复测只称定向通过，完整回归验收仍需full。

业务修复、测试维护、需求文档调整分别遵守用户授权；测试代码修复不是新增业务场景授权。确需扩展覆盖时再转新增路径。没有变更授权时只诊断和报告，不因读到失败报告就修改产品。沿用每case最多两次针对已证明测试缺陷的修复预算，仍无法验证则保留错误和阻塞，不持续扩大mock或放宽断言。

### 命令准备与执行复用

- 启动前一次核实cwd、项目脚本、运行时版本、依赖是否已安装以及参数转发规则。已有package脚本优先沿用；不要假定`--`会被正确转发，尤其检查测试名过滤是否真的选中了目标。Java按项目版本和缓存权限要求执行；权限失败走宿主审批，不反复更换缓存或解释器试错。
- 命令必须实际选中目标，检查执行数量与caseID；零测试、Setup失败或未到业务断言不是修复验证通过。原错误已消失但出现新错误时按新证据定位，不盲目重试同一命令。
- 长命令使用宿主会话句柄和合理等待窗口，一次收集相关任务结果；只有失败、停滞或需要用户操作时额外检查。输出等待窗口不是测试超时，不因短窗口到期重启任务。
- 有效通过结果关联当前输入；有改动时只重跑受影响检查，最后一次汇总Git diff、静态检查与测试结果。全量验收所需代码指纹、真实runner发现和清理门禁不因复用本地结果而省略。

## 需求、探索与代码

### 覆盖缺口门禁与模块补全

1. 标记需求未变/语义变化/不清楚，只对变化或不清楚预期澄清。未变但缺case时先列候选清单，需求审核与新增范围选择分别记录。requirement.review可省略（默认approved），显式pending/rejected仍阻止关联case成为合格代码；历史审核不代表本轮已授权新增。
2. 新增覆盖及完整回归核对需求→计划、计划/coverage→真实独立test、发现→coverage三向差集；仅修复或定向复测只核对相关映射。复用有效清单，变化或缺失才补读对应来源。新增覆盖用一份模块清单汇总“尚未完成自动化X项”，逐模块列数量、case与缺口原因；需求尚未建case时为候选指定ID，同一case的多项原因只计一项。不能确定的数量标明待核实。
3. 新增路径将清单交给用户选择：**按模块新增**（指定模块，仅补这些case）、**全部新增**（补齐当前清单）、**本轮跳过新增**（只跑已有自动化）。有可用选择组件时使用，否则直接征询。用户已明确只回归则直接记录跳过新增；已有覆盖当前清单的本轮授权直接登记并复用，旧轮决定不继承。普通“开始测试/执行全部测试”不决定新增范围；没有新增决定可继续已有回归，新增场景探索、fixture及代码等待有效范围授权，沉默或超时不授权新增。
4. 将用户答复原话、清单、所选模块与未选缺口写入同一份证据，通过automation_expansion登记。每个获选模块就绪后连续推进ego-browser探索→独立Playwright代码→fixture→受影响验证；同一探索者写代码。按独立写集并行，同文件互斥，已有合格case可同步回归。清单外新增缺口再次确认；所选范围内的既有授权持续有效。
5. 新增路径默认收尾全量现有自动化；用户限定定向复测时按指定目标交付，不扩大执行范围或声称完整验收。精确报告本轮选择和剩余缺口，长期coverage保留真实状态；现有自动化通过与需求全覆盖分别表述。脚本核对的是本地登记与范围，执行者须在新增场景代码、派发新增任务或对应浏览器探索之前落实用户选择。

新探索首个代表流程用独立数据执行两次且分别验证清理；稳定回归通常一次。每case最多两次针对已证明测试代码缺陷的修复，不改预期或吞异常制造通过。产品差异、环境/认证失败、真机缺失各自留因，业务代码修复另行授权。

### 稳定回归与探索重叠

首个获准新增的模块就绪即探索/编码，有效稳定case就绪即回归。新增任务以用户选择的清单和范围为前置；既有合格回归可与缺口盘点或选择等待并行。该流程实际来源、预期审批、身份、fixture和资源安全条件继续核验；报告清理是对应CLI前置，不阻挡无关只读页面探索。执行读集包含传递fixture/辅助代码/配置/认证输入，与探索写集冲突时只串行相关任务。

## 程序化快车道（SHOULD）

来源快检、盘点、清理、配额、绑定及报告由主Agent直接做，不为确定性机械步骤派Agent/写重复摘要。连续3次冗余、非业务本地调用均未推进任何已就绪工作时停止重复协调，转向就绪任务或已知阻塞；必要init/来源/清理/有效诊断不计入。无需新增汇报/计时墙，不能据此跳安全检查或强杀清理。

- **复用**：preflight汇总已有真实观察/发现及模块差集；未提供项保持unknown，外部补齐后直接record对应事件，不重复本地盘点。record/collect默认简报足够接下一步，--full只在需要清单/全局诊断时使用。简报global_residuals_checked=false不是无残留。
- **清理**：首次回归一次prepare-regression，后续复用；扩scope补范围。异常一次定位，输入未变不重复调用/现场改工具，相关范围等待、独立工作继续。
- **配额/租约**：实际浏览器/CLI前acquire，不先重复budget/ready；teardown后release。由上次claim判断租约，预计长操作超期才renew；等待不超过剩余租期一半，真过期才恢复。具体冲突/资源/旧租期见契约。
- **收尾**：attempt-bundle从真实command.json和原生报告生成标准事件，每CLI立即collect-results，不再重复import/validate。validate-input、attempt-check仅在新字段或诊断时使用；全部完成一次merge、登记验收、report。有差异才定点核对，不重复同输入试错。
- **沟通**：新增覆盖且无有效选择时征询范围；明确只回归不再征询。其他决定围绕真实歧义、审批范围或新高风险写操作。必要人工等待立即持久化并通知，机械状态不逐阶段汇报。

## 并行和数据边界（MUST）

默认Agent最多4，探索浏览器+全部Playwright workers共享浏览器上限4，按资源/冲突收紧，未知资源从一个浏览器开始；显式较小配置优先。按真实读写集/服务端冲突组局部串行，读读可共享；不同context或同账号不自动决定隔离/冲突。配额和实际suite都须落实冲突约束。

既有业务数据只读。专用E2E资源按intent→准确ID/owned证明→逆依赖teardown→absence check；创建/删除未知先核实，不猜ID/前缀、不重复创建。人工等待/认证恢复见[浏览器与认证](browser-and-auth.md)，资源继承和安全回收见[契约](contracts.md)。

## 回归及结束

新增覆盖或完整回归在开发期受影响验证后，收尾全量现有自动化；稳定且未修改代码的已有回归可直接执行full。定向复测按用户目标结束，仅本地修复不运行此收尾流程；未测范围如实列出，不能为获得fully_accepted扩大任务或伪造完整性。不能运行的目标逐项列阻塞，不静默过滤。真实E2E新run每CLI须JSON+blob、正常有HTML，最终全轮原生合并；旧run JSON-only仅受限兼容。原始失败/retry/Setup/未执行和映射要求统一见[报告](reports.md)，不能用最终pass隐藏首次失败。

终态按真实证据登记fully_accepted/completed_with_failures/partially_blocked等，区分需求覆盖与执行、UI/API、模拟/真机、local/CI。现有自动化全过不等于需求覆盖齐全；计时复用runner/阶段区间及配额登记峰值，未知null，空档unknown，不造逐动作计量文件或固定提速承诺。
