# 工作流：慢判断，快程序

先按[技能入口](../SKILL.md#先选路径)选择路径。确定性任务先看[直接完成](#确定性任务直接完成)，工作流绑定及需求验收看[最小命令顺序](#最小命令顺序)，按报告修复看[快车道](#按报告修复快车道)；字段/安全细节查[契约](contracts.md)，执行/合并细节查[报告](reports.md)。MUST为安全/证据门禁；SHOULD为默认效率路径，必要的诊断和恢复可以调整顺序，但不跳门禁。

## 确定性任务直接完成

“把这些case再跑一次”“只跑自动化”“确认需求是否变更”按本轮字面目标完成；只对会影响执行、预期或数据安全的真实疑点补证。

### 已有自动化复测

1. **锁定输入**：目标由用户指定，或从最近一份相关报告提取最终失败项；已给ID时无需再遍历历史run。一次核实项目命令、目标spec、认证及数据夹具的必要运行条件。沿用当前代码，不改断言、覆盖映射、需求版本或产品代码。
2. **一次发现并执行**：使用同一组spec/project/grep选择条件运行一次`--list`，核对业务目标集合及实际Setup。`--grep`匹配包含文件和project的完整测试名；caseID转义并限定结束分隔符，不在caseID前误加开头锚点。零目标、漏项或多选时只修正选择条件；匹配后立即执行，保留Setup依赖。目标为`SP-01.1`和`SP-02`、标题以空格分隔caseID时，例如`--grep '(SP-01\.1|SP-02)( |$)'`，分隔符以实际项目为准。
3. **连续完成机械步骤**：优先沿用项目runner；一次设置独立JSON/HTML/blob及输出目录、必要资源绑定和配额，真实CLI结束后完成teardown、资源核对及配额释放。夹具需要run/owner等输入时复用有效真实绑定，或一次准备必要绑定；不伪造ID、审核或来源观察。执行期间冻结计划和映射，已有活跃进程先接回会话，不重复启动。
4. **结果就绪即交付**：核对退出码、逐case结果、重试、未执行和清理状态，交付原生报告路径，分别说明整轮等待与CLI时长。单次原生HTML可直接交付；工作流多bundle按既有契约合并。没有本轮来源核验时，结论限定为当前代码的复测结果。

来源新鲜度、审核版本或历史映射等登记问题只影响对应验收声明。绑定失败最多进行一次有依据的定点修正；仍失败则分别说明原始测试结果和未完成登记，保留产物及安全恢复点，释放可安全释放的占用。不要循环读源码、改版本或重跑业务测试来消除登记错误。用户要求完整需求验收时，继续满足原有全部门禁。

### 确认来源是否变更

按所指来源复用既有读取工具，一次取得当前版本及可靠内容指纹，与同口径基线比较。版本变化不等于业务语义变化；指纹不同则定位相关内容差异，缺基线或不可读则说明无法确认。交付比较结果后结束，不自动启动浏览器、测试、审批或覆盖补全。认证失败按实际来源技能处理，只阻塞依赖该来源的任务。

### 程序接管确定性区间

项目runner优先，其次在一次宿主调用中用安全argv和结构化JSON串接已有命令；不为执行固定命令新增Agent或交接文件。主Agent只提供项目路径、目标、已确定的运行条件、完成标准和授权边界。程序连续完成一次发现→准备/环境检查→领取配额→真实CLI→teardown/缺席核验→释放配额→采集→收尾。正常只返回计数、差异、产物入口和释放结果。

一次执行中先验证所有必需环境变量，包括夹具的run/owner/marker/attempt目录，使用实际接管输出，不能猜默认值；配额`writable_paths`用项目相对路径，owner_pid绑定实际存活的runner进程（Python runner用`os.getpid()`，有意绑定其父进程时用`os.getppid()`，不依赖shell的PPID环境变量）。配额拒绝、身份失效或清理异常时停止相关启动并保留真实状态，不盲目继续。

同一项目后续批次复用已验证的runner及结构化输入，只调整目标、bundle和实际配额，不逐轮生成临时执行脚本或重新研究字段。新增路径的代表验证也先用真实选择参数发现目标；该过滤清单用于定向执行，不能替代收尾full所需的未过滤完整发现。探索占用在任务完成并清理后释放；仍需并行探索时按实际总预算为CLI留出workers，配额拒绝仅诊断一次真实持有者，不盲目重试或撤销活跃租约。

工具返回会话句柄时等待同一程序，不拆出新run或重复启动；使用能保持进度沟通的等待窗口。资源状态与teardown由夹具/项目适配器产生真实回执，未知创建结果交Agent处理，脚本不猜测删除。收到正常完成摘要立即交付，只有新的实际错误才读相应日志。

## 最小命令顺序

以下是runner内部动作清单，应合成连续程序调用，不按行返回Agent。用于需要工作流绑定或需求验收的真实E2E；确定性请求先走[直接完成](#确定性任务直接完成)。已有回归复用有效输入，明确只回归时直接登记跳过新增；仅本地修复见[快车道](#按报告修复快车道)。旧start/renew路径继续可用，已有就绪输入不重复预检。实际CLI/merge不是workflow代跑。

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
# 从真实command.json及JSON/HTML/blob一次生成并采集，失败保留原始证据
python3 "$WF" attempt-collect --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle "$BUNDLE"
# 所有CLI已结束、配额已释放、资源已核对后，输入真实验收决定；一次收尾并直接交付
python3 "$WF" finalize --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$FINALIZE_INPUT"
```

### 输入准备与命令边界

- **初始化**：init仅补spec/coverage，按真实需求确认配置、固定范围和映射；缓存写入前核实Git忽略。doctor只读诊断，不自动修复或批准。
- **SOURCE_EVENTS**：实际读取来源并保存快照，实际审批后保存决定/范围证据，再写单事件或数组JSON。恢复还需recovery_review；已有有效baseline未变时只登记source:unchanged，不重做批准。
- **新增范围**：新增覆盖时将模块缺口清单交给用户选择，用automation_expansion登记真实答复、完整非空清单和范围。明确只回归则保存用户原话与跳过新增决定，不重复征询；完整清单就绪才登记skip事件，无缺口不造事件。清单待核实不阻挡已有合格测试执行，也不以部分清单冒充完整清单或新增授权；完整回归收尾仍核对实际发现。
- **探索/代码**：在获准范围内补独立测试与真实认证/数据fixture，以case事件更新coverage和完成状态；待选择或未获选的缺口保持待补，已有合格测试继续回归。
- **实际CLI**：先prepare-regression、领取真实workers配额，再运行spec批准的项目命令。每次独立attempt目录，用JSON/HTML/blob reporters及输出目录参数（详见[报告](reports.md#每次命令独立保存)）；记录真实起止/退出码，失败也teardown、核对资源和释放配额。
- **结果采集**：真实回执就绪即调用attempt-collect，程序内部生成bundle并走collect-results原有门禁；多CLI换bundle_id分别采集。complete/unresolved由程序核对，不为登记问题重跑测试。
- **FINALIZE_INPUT**：使用[批量入口](local-tools.md#确定性批量入口)的结构化输入，声明实际验收终态及full/定向完成标准，附真实资源/issue回执；需要合并时提供项目本地Playwright启动argv，已有有效merge复用。finalize内部依次合并、登记、report核验、release，返回成功摘要后不再检查同一批证据。无目标以明确阻塞终态关闭，不造空bundle。

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

新增共享fixture或复杂交互时，先完成最小代表流程的代码、必要映射和安全夹具，立即定向验证创建、交互、持久化及清理，再扩展同类case。代表流程默认一次；新fixture涉及唯一约束或逆依赖清理时默认用独立数据跑两次，以暴露重复创建和清理残留。有真实竞态证据、修复影响或用户要求稳定性验证时，才追加受影响目标的验证，使用独立数据并分别核验清理。不要把`--repeat-each`作为固定前置；稳定且输入未变时不重复代表轮。每case最多两次针对已证明测试代码缺陷的修复，不改预期或吞异常制造通过。产品差异、环境/认证失败、真机缺失各自留因，业务代码修复另行授权。

### 稳定回归与探索重叠

首个获准新增的模块就绪即探索/编码，首个代表case按[上节](#覆盖缺口门禁与模块补全)定向验证。其他独立写集继续探索/编码；共享fixture尚在修改时先冻结该依赖及代表spec，或使用明确隔离的工作副本，不能一边改同一读集一边运行。新增任务以用户选择的清单和范围为前置；既有合格回归可与缺口盘点或选择等待并行。该流程实际来源、预期审批、身份、fixture和资源安全条件继续核验；报告清理是对应CLI前置，不阻挡无关只读页面探索。执行读集包含传递fixture/辅助代码/配置/认证输入，与探索写集冲突时只串行相关任务。

## 程序化快车道（MUST）

按[入口的执行效率约束](../SKILL.md#执行效率should)组织整轮：Agent处理不确定性，程序处理已确定步骤。每个连续机械区间一次调用，不逐步计划、汇报、派Agent、轮询或读回大对象。底层命令是组合入口的实现/诊断接口，不能与组合入口重复执行。

- **接管/来源/差集**：必要读取合批；preflight汇总现有真实观察和发现，来源正文只在缺失或可靠指纹变化时加载。没有新输入不重复盘点，版本/指纹比较由程序完成，语义变化才交Agent。
- **准备/执行**：首次prepare-regression，后续复用；项目runner一次设置环境/资源绑定、领取真实配额、CLI、teardown/核验及释放。必要的初始化和配置读取放同一程序，启动前缺项立即停止，不能先失败一轮才补环境。
- **采集/收尾**：每次CLI只用attempt-collect；全部CLI完成只用finalize。原始失败和重试保留在同一次合并中，完整性与通过率分别检查，验收决定不能由“exit0”猜出。
- **恢复**：优先接回真实进程/已完成checkpoint；报告或登记错误使用原始产物定点修复，不重跑业务。组合入口失败时只读其指定证据，不重新遍历全部来源、用例、附件和历史run。
- **交付**：成功摘要足够答复，不再status/validate/逐case或资源扫描；详细证据在报告内。真实业务异常或新增范围交用户/Agent，其余机械步骤连续完成。

## 并行和数据边界（MUST）

默认总共4个Agent，包含主Agent；主Agent占1个名额，默认最多3个子Agent。4是起始值而非固定上限；有独立任务且宿主有槽位时，可增加至5个及以上。主Agent依据近期整机CPU和内存使用率逐步增减：两项均低于80%才可扩容，任一达到80%则暂停新增并减少后续派发，已有任务正常执行安全清理，不强杀。每次扩容前及新批次派发时复查采样；缺少可靠采样时保持默认或收紧，不把CPU核数、load average或可用内存字节直接当作总体使用率。通过宿主实际槽位和`scheduling.py`的concurrency覆盖值落实调整，`pressure=true`暂停新配额；默认4是起点，宿主槽位、独立任务数量、数据冲突仍是约束。总使用率可能随外部进程变化，采样不能承诺持续低于阈值。探索浏览器+全部Playwright workers共享默认浏览器上限4，按资源/冲突收紧，未知资源从一个浏览器开始；Agent扩容不自动扩大浏览器配额，显式较小配置优先。按真实读写集/服务端冲突组局部串行，读读可共享；不同context或同账号不自动决定隔离/冲突。配额和实际suite都须落实冲突约束。

既有业务数据只读。专用E2E资源按intent→准确ID/owned证明→逆依赖teardown→absence check；创建/删除未知先核实，不猜ID/前缀、不重复创建。人工等待/认证恢复见[浏览器与认证](browser-and-auth.md)，资源继承和安全回收见[契约](contracts.md)。

## 回归及结束

新增覆盖或完整回归在开发期受影响验证后，收尾全量现有自动化；稳定且未修改代码的已有回归可直接执行full。定向复测按用户目标结束，仅本地修复不运行此收尾流程；未测范围如实列出，不能为获得fully_accepted扩大任务或伪造完整性。不能运行的目标逐项列阻塞，不静默过滤。工作流新run每CLI须JSON+blob、正常有HTML，最终全轮原生合并；直接执行交付原生结果并说明登记状态，旧run JSON-only仅受限兼容。原始失败/retry/Setup/未执行和映射要求统一见[报告](reports.md)，不能用最终pass隐藏首次失败。

终态按真实证据登记fully_accepted/completed_with_failures/partially_blocked等，区分需求覆盖与执行、UI/API、模拟/真机、local/CI。现有自动化全过不等于需求覆盖齐全；计时复用runner/阶段区间及配额登记峰值，未知null，空档unknown，不造逐动作计量文件或固定提速承诺。
