# 工作流：慢判断，快程序

首次使用看[最小命令顺序](#最小命令顺序)；字段/安全细节查[契约](contracts.md)，执行/合并细节查[报告](reports.md)。MUST为安全/证据门禁；SHOULD为默认效率路径，必要的诊断和恢复可以调整顺序，但不跳门禁。

## 最小命令顺序

日常命令见[技能入口](../SKILL.md#最小命令顺序)：init→preflight→record来源/审批→有缺口时取得并record新增范围选择→prepare-regression→attempt-init→配额acquire→实际CLI/teardown→配额release→attempt-bundle→collect-results→实际merge→record合并/验收→report→release。旧start/renew路径继续可用，已有就绪输入直接复用，不重复预检。字段见[本地辅助入口](local-tools.md)。实际CLI/merge不是workflow代跑。

### 输入准备与命令边界

- **初始化**：init仅补spec/coverage，按真实需求确认配置、固定范围和映射；缓存写入前核实Git忽略。doctor只读诊断，不自动修复或批准。
- **SOURCE_EVENTS**：实际读取来源并保存快照，实际审批后保存决定/范围证据，再写单事件或数组JSON。恢复还需recovery_review；已有有效baseline未变时只登记source:unchanged，不重做批准。
- **新增范围**：将模块缺口清单交给用户选择按模块新增、全部新增或本轮跳过新增。用automation_expansion事件登记真实答复、清单和范围；明确覆盖当前清单的本轮授权可复用。
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

## 需求、探索与代码

### 覆盖缺口门禁与模块补全

1. 标记需求未变/语义变化/不清楚，只对变化或不清楚预期澄清。未变但缺case时先列候选清单，需求审核与新增范围选择分别记录。requirement.review可省略（默认approved），显式pending/rejected仍阻止关联case成为合格代码；历史审核不代表本轮已授权新增。
2. 每轮核对需求→计划、计划/coverage→真实独立test、发现→coverage三向差集，用一份模块清单列ID、代码/审批状态与阻塞。汇总“尚未完成自动化X项”，逐模块列数量、case与缺口原因；需求尚未建case时为候选指定ID，同一case的多项原因只计一项。不能确定的数量标明待核实。
3. 主Agent将清单交给用户选择：**按模块新增**（指定模块，仅补这些模块列出的case）、**全部新增**（补齐当前清单）、**本轮跳过新增**（只跑已有自动化）。用宿主的选择组件提供三个选项；无选择组件时直接征询明确答复。用户回复前只继续独立的只读盘点和已有回归；新增场景探索、fixture及代码等待范围选择。普通“开始测试/执行全部测试”不决定新增范围，沉默或超时保持待选择。本轮已有明确覆盖清单的授权直接登记并复用，旧轮决定不继承。
4. 将用户答复原话、清单、所选模块与未选缺口写入同一份证据，通过automation_expansion登记。每个获选模块就绪后连续推进ego-browser探索→独立Playwright代码→fixture→受影响验证；同一探索者写代码。按独立写集并行，同文件互斥，已有合格case可同步回归。清单外新增缺口再次确认；所选范围内的既有授权持续有效。
5. 跳过新增或仅选部分模块时，仍收尾全量现有自动化，精确报告本轮新增选择和剩余缺口。新增选择属于本轮执行范围，长期coverage保留真实状态；现有自动化通过与需求全覆盖分别表述。脚本核对的是本地登记与范围，执行者须在编辑代码、派发新增任务或启动对应浏览器探索之前落实用户选择。

新探索首个代表流程用独立数据执行两次且分别验证清理；稳定回归通常一次。每case最多两次针对已证明测试代码缺陷的修复，不改预期或吞异常制造通过。产品差异、环境/认证失败、真机缺失各自留因，业务代码修复另行授权。

### 稳定回归与探索重叠

首个获准新增的模块就绪即探索/编码，有效稳定case就绪即回归。新增任务以用户选择的清单和范围为前置；既有合格回归可与缺口盘点或选择等待并行。该流程实际来源、预期审批、身份、fixture和资源安全条件继续核验；报告清理是对应CLI前置，不阻挡无关只读页面探索。执行读集包含传递fixture/辅助代码/配置/认证输入，与探索写集冲突时只串行相关任务。

## 程序化快车道（SHOULD）

来源快检、盘点、清理、配额、绑定及报告由主Agent直接做，不为确定性机械步骤派Agent/写重复摘要。连续3次冗余、非业务本地调用均未推进任何已就绪工作时停止重复协调，转向就绪任务或已知阻塞；必要init/来源/清理/有效诊断不计入。无需新增汇报/计时墙，不能据此跳安全检查或强杀清理。

- **复用**：preflight汇总已有真实观察/发现及模块差集；未提供项保持unknown，外部补齐后直接record对应事件，不重复本地盘点。record/collect默认简报足够接下一步，--full只在需要清单/全局诊断时使用。简报global_residuals_checked=false不是无残留。
- **清理**：首次回归一次prepare-regression，后续复用；扩scope补范围。异常一次定位，输入未变不重复调用/现场改工具，相关范围等待、独立工作继续。
- **配额/租约**：实际浏览器/CLI前acquire，不先重复budget/ready；teardown后release。由上次claim判断租约，预计长操作超期才renew；等待不超过剩余租期一半，真过期才恢复。具体冲突/资源/旧租期见契约。
- **收尾**：attempt-bundle从真实command.json和原生报告生成标准事件，每CLI立即collect-results，不再重复import/validate。validate-input、attempt-check仅在新字段或诊断时使用；全部完成一次merge、登记验收、report。有差异才定点核对，不重复同输入试错。
- **沟通**：有自动化缺口时征询新增范围；其他决定围绕真实歧义、审批范围或新高风险写操作。必要人工等待立即持久化并通知，机械状态不逐阶段汇报。

## 并行和数据边界（MUST）

默认Agent最多4，探索浏览器+全部Playwright workers共享浏览器上限4，按资源/冲突收紧，未知资源从一个浏览器开始；显式较小配置优先。按真实读写集/服务端冲突组局部串行，读读可共享；不同context或同账号不自动决定隔离/冲突。配额和实际suite都须落实冲突约束。

既有业务数据只读。专用E2E资源按intent→准确ID/owned证明→逆依赖teardown→absence check；创建/删除未知先核实，不猜ID/前缀、不重复创建。人工等待/认证恢复见[浏览器与认证](browser-and-auth.md)，资源继承和安全回收见[契约](contracts.md)。

## 回归及结束

开发期受影响验证后，收尾全量现有自动化；不能运行的目标逐项列阻塞，不静默过滤。新run每CLI须JSON+blob、正常有HTML，最终全轮原生合并；旧run JSON-only仅受限兼容。原始失败/retry/Setup/未执行和映射要求统一见[报告](reports.md)，不能用最终pass隐藏首次失败。

终态按真实证据登记fully_accepted/completed_with_failures/partially_blocked等，区分需求覆盖与执行、UI/API、模拟/真机、local/CI。现有自动化全过不等于需求覆盖齐全；计时复用runner/阶段区间及配额登记峰值，未知null，空档unknown，不造逐动作计量文件或固定提速承诺。
