# 断点调度与有限工作包

用于需要逐case观察复用的长回归、中断续跑和失败确认；项目已有恢复能力时优先复用。程序负责日志、输入指纹、领取、修复历史和分组；Agent只诊断未知根因、审核依赖闭包及编写修复。以下入口独立于`workflow.py`，不启动产品测试、不修改验收状态、不授予权限、不替代浏览器配额

进度按[用户可见汇报](workflow.md#进度汇报与长任务等待)执行；已接入sidecar时可用新增`case_end`观察更新本批结束数，不为进度另装适配器或改变retry/repeatEach。`plan.counts.ready/reused_observations/blocked`是调度资格统计；blocked包含在途/清理未核验、容量等待等情况，不能直接当失败数或未执行数。恢复复用观察、本轮结果、未决清理分别展示；观察到test结束不代表资源已释放或正式报告已绑定。

## 一次接入项目runner

先核实suite是否适用：该sidecar每attempt每case只接受一个结果，要求retry=0且repeatEach=1。项目原有重试或重复执行属于本轮目标时，保留原设置，使用原生JSON/blob/HTML及正常attempt-collect，不强行接入sidecar或宣称逐case复用。配额、冲突组及[残留派发门禁](#跨run残留门禁)照常执行。只有项目已采用该单次模型或用户明确同意改变执行语义时才接入，不为长回归自动关闭重试/重复执行。

适用的项目runner在真实发现后生成精确清单，接入一次即可复用。保留原生JSON/blob/HTML reporter，并追加`scripts/checkpoint_playwright_reporter.mjs`作为观察sidecar；使用attempt-init的reporter参数时，在输入`extra_reporters`传该随附脚本的绝对路径，避免CLI覆盖配置后漏掉sidecar，详见[Attempt准备](local-tools.md#attempt准备与事件生成)。每次确认或修复复测领取独立attempt。

```json
{
  "environment_id": "deployment-config-v1",
  "cases": [{
    "case_id": "A",
    "target": {
      "file": "test/a.spec.ts",
      "project": "chromium",
      "title_path": ["", "chromium", "test/a.spec.ts", "group", "A"]
    },
    "inputs": ["test/a.spec.ts", "test/shared.ts", "playwright.config.ts"],
    "dependencies": ["login"],
    "conflicts": ["tenant-config"],
    "reviewed": true
  }]
}
```

- `target.title_path`是该项目实际Playwright `TestCase.titlePath()`的完整数组，包括describe层级；示例不是所有项目的固定结构。发现转换器必须与实际版本对齐，不只取叶子标题。Reporter在onBegin核对实际suite、歧义和repeatEach
- `target.project`取真实project名称，未命名时为`""`，不补造chromium等名称；文件、project及完整title_path共同决定身份。
- `inputs`包含测试及审核过的传递依赖、fixture、配置和影响预期的来源缓存。漏列依赖不能证明复用有效；`reviewed:true`仅声明闭包经过审核，不替代workflow用例审批。共享输入每次plan只哈希一次
- `environment_id`是部署/配置基线标识，不包含凭据。相关远端环境变化时创建新checkpoint；清单初始化后不可变。checkpoint不自动侦测远端变更
- `dependencies`用于登录、Bootstrap、helper等依赖熔断；`conflicts`用于租户共享配置、账号或数据冲突。独立组可并发，共享组串行，配额仍由`scheduling.py`管理

```sh
python3 "$SKILL/scripts/checkpoint.py" init --project "$PROJECT" \
  --journal test/.cache/checkpoint.jsonl --input "$MANIFEST"
python3 "$SKILL/scripts/checkpoint.py" plan --project "$PROJECT" \
  --journal test/.cache/checkpoint.jsonl --workers 4
```

runner从`ready`领取，不自行重跑`reused_observations`。`claim`在跨进程锁内重新核对容量、冲突与指纹；workers必须统一为该checkpoint的实际并发上限。领取示例：

```json
{"attempt_id":"batch-1","case_ids":["A"]}
```

用`claim --input REQUEST.json`写入上述请求，再执行已获准的测试。完整顺序为：现有preflight/attempt-init→配额与claim→真实CLI→ingest→资源清理核验与close→teardown后释放配额→原生attempt-collect。各次claim只包含一个可并行波次，不把串行队列整批占为worker。启动失败也保留占用，runner核清无创建结果和清理义务后才能close

Reporter环境变量由runner传入子进程，不打印：

| 变量 | 值 |
| --- | --- |
| `E2E_CHECKPOINT_PROJECT` | 规范化项目绝对路径 |
| `E2E_CHECKPOINT_ATTEMPT` | 当前独立attempt ID |
| `E2E_CHECKPOINT_MANIFEST` | 当前清单绝对路径 |
| `E2E_CHECKPOINT_STREAM` | 项目test/.cache下全新JSONL绝对路径 |
| `E2E_CHECKPOINT_SETUP_PROJECTS` | 可选，明确排除的认证Setup项目名，逗号分隔 |

每个onTestEnd写入并fsync，不保存原始错误或请求。sidecar要求实际`result.retry=0`；非0写入mapping_error并阻止整批摄取，不将重试通过覆盖旧失败。checkpoint init只校验manifest，不修改Playwright配置；适用性必须在执行前确定。误接入时保留真实原生产物，修复集成并复用原生采集，不为获得sidecar结果而重跑业务。完整映射冲突和未知目标同样使sidecar失败；一个attempt每case只允许一个结果。

## 中断恢复与依赖熔断

中断全量批次可能跳过fixture teardown，留下已创建或创建结果unknown的资源；数量按实际账本核实，不等于未完成case数。下一批相关写入会被残留门禁阻断，反复新建run不会消除这些义务。除继续等待原进程外，决定中断时由runner保留收尾入口及证据；真实中断后先按[恢复流程](cleanup.md#执行与恢复)收敛实际资源、落恢复overlay并提交cleanup_state回读索引，再启动相关下一批。明确只读或与残留无交集的任务仍按门禁判断。门禁阻断记为blocked/未执行，不作为业务断言失败。

中断后分别核验项目fixture的锁、身份池租用及资源残留；配额reap或原CLI退出不代表三者已恢复。身份Bootstrap锁超时先按[身份池锁与中断恢复](browser-and-auth.md#身份池锁与中断恢复)区分活锁、未知持有者与死哨兵，完成互斥恢复和资源对账后再派发相关下一批，不靠新run、增加worker或反复重试跨过。

长CLI在spawn前持久化本次command、真实开始时间、独立输出路径和退出码null，启动后补实际进程标识，退出后再补结束时间与真实退出码；只有结束时才写回执会在宿主中断时丢失边界。使用宿主支持的可恢复会话，需脱离宿主进程组时由项目runner管理租约续期和清理生命周期，不能只后台启动后放弃管理。

先查真实进程。仍在运行则接续等待，不重启。会话句柄丢失、观察等待超时或探测EPERM只表示状态未知，不等于进程已死；核对存活进程及最近产物再决定。进程已退出时先保留原生文件、sidecar和真实退出回执，再摄取完整观察：

```json
{"attempt_id":"batch-1","stream":"test/.cache/batch-1.jsonl"}
```

以`ingest --input REQUEST.json`摄取；事件ID幂等，整批先验证再追加。partial/corrupt尾部停止摄取，不截断、不猜修。JSONL journal带序号、hash链、跨进程锁和fsync；进程崩溃不自动释放原占用。runner确认逆依赖清理与absence/允许的保留结果后才能record：

```json
{"type":"close","event_id":"close-batch-1","attempt_id":"batch-1","cleanup_verified":true,"evidence":["test/.cache/cleanup.json"]}
```

正常结束时，`close`要求所有领取的case均已摄取case_end；缺少结果会拒绝close并保留占用。允许先完成实际清理，但记录close必须在结果摄取之后

真实中断或启动失败导致部分目标没有终态时，先核实进程已退出并摄取全部可用结果，再record显式中断对账事件：

```json
{"type":"interruption","event_id":"interrupted-batch-1","attempt_id":"batch-1","unfinished_case_ids":["B"],"process_exited":true,"results_reconciled":true,"evidence":["test/.cache/interruption-reconciliation.json"]}
```

`unfinished_case_ids`必须恰好等于该attempt尚无case_end的目标集合。证据记录进程退出及可用原生/sidecar结果的核对情况；仍有未核清结果时保留占用，不用中断事件跳过摄取。事件只标识未完成，不制造passed/failed，也不计一次失败确认。随后完成清理核验并close，未完成目标才回到pending

`close`前所有case继续占用冲突组；输入未变的passed只作续跑观察复用，失败须先诊断。无资源的纯UI流程也由runner记录无写入/无清理义务的真实依据。`cleanup_verified`不是工具代替用户批准危险写操作

登录429、Bootstrap500或helper不可用等已明确的依赖故障，应由runner依据结构化错误立即发block，停止启动后续依赖case，不等数十条相同失败。未知错误不自动猜依赖。例：

```json
{"type":"block","event_id":"login-down-1","dependency":"login","evidence":["test/.cache/login-diagnostic.json"]}
```

通过`record`追加；熔断跨重启保留，仅阻断相关dependencies。已运行CLI不会被工具杀死，runner负责停止后续派发和必要的受控中断。只读健康核验及环境恢复有证据后，record对应`unblock`。权限/安全锁必须有相应有效授权，普通继续测试或工具升级不能替代授权；已有有效授权直接复用，不逐次重问。框架缺陷或旧恢复标记由主Agent先按[自动解阻](workflow.md#框架自修复与自动解阻)修复、核验，再登记unblock，不直接删除block历史

## 跨run残留门禁

runner在资源创建结果未决或清理失败时，立即调用`checkpoint.py record`登记`cleanup_state`，成功后才进入下一波claim；已有`cleanup_state`适配器将自身账本归一化到此事件。该入口只保存本地证据，不执行删除，也不自动读取业务fixture。旧账本由adapter按原始run及准确资源ID一次性补登记；索引上线本身不会发现尚未导入的历史残留。

清单可增加`resource_environment`和`resource_run_id`。前者是稳定的测试环境+租户/数据域别名，不随部署、checkpoint或run变化；后者在同轮所有checkpoint间保持一致。缺省时登记分别沿用`environment_id`和journal路径，仅兼容旧接入。工具内部记录`environment_is_stable`，不接受事件自行提供；旧索引缺此字段或生产方未声明稳定环境的未决资源，对所有环境的相关写操作保守阻断，即使新consumer已声明稳定别名也不能绕过。改用新清单重复登记同一身份不能升级旧记录的环境可信度；须在原journal/环境命名空间按准确ID和实际核验凭据关闭旧残留。consumer未声明稳定环境时同样检查全部环境；只有明确稳定的记录才按环境隔离。

每个case及工作包item可增加`resource_access`，例如：

```json
{"mode":"write","kinds":["product"],"dependencies":["category"]}
```

`kinds`为可能写入的资源类型，`dependencies`为这些写操作依赖的资源类型。审核完整fixture闭包后填写。`mode:readonly`且`kinds:[]`的case继续；write与未决kind无交集也继续。缺省resource_access的case在相关环境存在残留时保守阻断；显式两个空数组表示没有相关资源依赖，不代表可跳过其他权限或冲突门禁。

登记示例，`evidence`引用已脱敏的现有清理回执：

```json
{"type":"cleanup_state","event_id":"category-cleanup-1","resources":[{"kind":"category","id":"70","status":"cleanup_failed","evidence":["test/.cache/cleanup-receipt.json"]}]}
```

资源status支持`intent/confirmed/ambiguous/cleanup_failed/absence_verified/retained_verified`。`test/.cache/resource-residuals.json`在项目共享锁内增量更新，按`resource_environment+kind+id`去重；不遍历历史run。索引先原子保存并fsync，再追加checkpoint，进程中断后仍阻断；相同journal+event_id幂等，不以旧事件重放覆盖新状态。索引损坏时停止派发并保留原件。

自动`cleanup_state`桥接由runner在创建结果未决及清理结果产生时实际执行：归一化准确资源ID、状态和证据，调用`checkpoint.py record`并检查退出码，在下一次相关写派发前确认索引已持久化；桥接失败时保留证据并阻断相关写操作。手工seed用于历史初始化，持续接入以实际桥接调用回执及索引回读为依据，仅seed加gate不代表自动桥接已生效。

资源producer接入或变更时，维护项目的规范账本适配清单，覆盖业务资源、临时身份及授权关系、规则/脚本、消息资源池和运行时数据义务。使用真实producer结构做离线契约测试，同时验证清理依赖消费者与残留桥接；runner终止补登复用此清单，避免仅扫描一种文件名或schema漏债。引用式账本的ID不重复计物理资源，停源/数据缺席与实体删除分别对账；消费者只接受规范数组时，由producer输出有绑定的独立cleanup gate，成功值由全部停源、数据清理及连接释放结果决定。旧格式缺字段不能直接补成功，恢复使用原件哈希及独立准确回读证明。

清理按依赖图推进：父资源等待准确子资源解除；一个分支失败后，仍收敛在途操作并清理已证明独立、安全的其他分支，保留所有失败与未决状态。例如规则失败不应跳过无引用且归属已核验的脚本。清理回归至少覆盖部分失败、unknown创建、依赖未解除、旧schema和连接释放失败，不以单个`cleanupComplete`标志代替完整证明。

恢复审核在派发前保存不可变快照，计划引用该快照及全部原始证据的准确路径和哈希。并行Agent的fixture验证、追加诊断写入新sidecar，不改写已被恢复计划或journal引用的审核文件；恢复结束核对证据未变。原件字节缺失时保留缺失边界，不用摘要或重新序列化冒充原件。

用户明确授权的独占数据域整体reset按[清理流程](cleanup.md)执行：授权scope和resource kinds内的当前完整盘点可覆盖原创建结果unknown，缺逐条旧receipt不一概阻断恢复；项目确认的共享/系统数据域仍按准确owned规则，保护基线由用户确认。普通case写派发门禁保持生效，恢复动作在原授权及互斥内进行，不借修改scope绕过。只有停源后各获准删除集合全分页归零、保护基线回读及运行时义务核验完成，才能生成scope absence。adapter将该证明映射到原环境/准确租户及kind下的相关ID或原unknown义务，实际提交cleanup_state并回读索引；不能静默清索引、改原账本或以test passed结案。现有接口仍逐准确ID/义务处理，未可靠实现scope证明映射的adapter保留阻塞；此说明不新增原生scope-reset命令或字段。

恢复仍走同一事件：status改为`absence_verified`，evidence中的JSON回读回执须匹配环境、kind、id、status，并含严格布尔值`verified:true`：

```json
{"resource_environment":"tenant-dev","kind":"category","id":"70","status":"absence_verified","verified":true}
```

这是`evidence`引用文件的顶层dict，必需字段不能包进`resources`数组或索引对象；`id`必须是字符串，`verified`必须是JSON布尔值true，不能用`1`或`"true"`。可增加来源证据字段，但不替代这些匹配字段。外层cleanup_state事件仍用`resources:[...]`；原始API响应单独保存，由项目adapter按[资源恢复契约](cleanup.md#项目资源恢复契约)判断后生成回执，不能手写true代替实际查询。

`retained_verified`还要求事件和回执的`retention`一致且为`pooled/disabled`，保留结果须满足原资源账本及授权要求。回执由runner依据实际absence查询或保留状态回读生成，附相应来源证据；工具核验匹配字段，不代替真实查询。test passed、close.cleanup_verified或普通依赖unblock均不改变此索引。共享owned路径仅关闭已核验的准确ID；专用reset路径须以完整scope absence映射到原残留义务后逐项关闭，scope外或映射不明的同kind残留继续阻断。

`shared_config`不允许保留关闭，也不能仅凭通用`verified:true`回执关闭。adapter须先在未决事件中以`original_snapshot`引用资源账本中真实、修改前的原值JSON；工具固定路径并计算规范化JSON的SHA256，后续跨run登记不得替换路径、清空基线或改写原件。历史原值暂缺时可先登记未决以阻断，但不能拿当前值冒充原值，也不能在关闭事件中首次补造基线。恢复后以`absence_verified`关闭（此处表示原值恢复，并非删除配置），事件另带`restored_snapshot`指向实际恢复后回读JSON，且匹配回执必须引用同一`restored_snapshot`。工具核验恢复值与已固定原值的规范化JSON一致；其他kind不接受这两个快照字段。例如先登记原值，再恢复回读后关闭：

```json
{"type":"cleanup_state","event_id":"config-pending","resources":[{"kind":"shared_config","id":"config-1","status":"cleanup_failed","original_snapshot":"test/.cache/config-original.json","evidence":["test/.cache/config-cleanup.json"]}]}
{"type":"cleanup_state","event_id":"config-restored","resources":[{"kind":"shared_config","id":"config-1","status":"absence_verified","restored_snapshot":"test/.cache/config-restored.json","evidence":["test/.cache/config-readback.json"]}]}
```

对应`config-readback.json`含`resource_environment/kind/id/status/verified`及`"restored_snapshot":"test/.cache/config-restored.json"`，身份与事件一致。只有文件比较通过不代表已执行真实恢复或查询，adapter仍须保存其来源证据。

plan/claim实时检查索引，阻断原因是`resource_cleanup_unresolved`；已运行的CLI不自动终止，runner停止后续相关写派发。work_packages沿用schema_version=2及`cleanup_review_no_blind_retry`，派生`cleanup_unknown:true`并保留`residual_kinds`约束，权限门禁仍优先。工作包顶层可增加同一`resource_environment`，输出`residual_index_sha256`用于快照归属；新证据后生成新包，实际启动仍须claim重查，不直接执行旧包。

报告按唯一资源而非DELETE次数统计。plan的`resource_residuals`分别给出`current_run_unresolved`（本轮首次登记且仍未决）、`historical_unresolved`（此前首次登记且仍未决）及两者之和`cumulative_unresolved`；`current_run_absence_verified/current_run_retained_verified`单列本轮核验关闭的资源，保留不计删除。本轮观察到的线上存量另列观察时间和范围，不与本轮新增或DELETE失败次数混算。历史导入须使用正确原始resource_run_id，否则首次登记归属不能代表真实创建轮次。

错误证据保留必要的method/route、HTTP status、业务status及脱敏msg；先移除凭据、请求头、账号等敏感值。cleanup_state仅接受资源身份、状态和证据引用，不接收原始msg/响应体。索引及其未决/恢复证据（包括共享配置原值、恢复回读快照及来源证据）属于持续状态；跨run回收按[证据保护](#跨run证据与回收)显式保护。配置快照仅存授权范围内的可比较字段，不写入凭据。

### 非checkpoint runner的强制pre-dispatch

`workflow.py/workflow_tools.py attempt-init`不自动读取此索引。没有checkpoint.claim的runner（包括直接run-attempt.py路径）必须在每次产品CLI启动前调用下列门禁，非0退出码停止派发，不捕获后继续。在runner固定入口接入后，由调用者传入审核过的整批fixture资源scope；未声明时传仅含resource_environment的对象，在已有残留时fail closed。readonly必须明确声明，不根据用例名称猜测。门禁放行仅表示无残留冲突，不替代原授权/审批。

```sh
python3 "$SKILL/scripts/checkpoint.py" gate --project "$PROJECT" --input "$SCOPE" && \
  python3 "$RUNNER" "$@"
```

`SCOPE`为`{"resource_environment":"稳定环境别名","resource_access":{"mode":"write","kinds":["device"],"dependencies":["product","category","protocol"]}}`；只读改为`mode:readonly`且两个数组为空。gate无需journal，读取项目同一持久索引，冲突输出`allowed:false`并退出2；只读/独立scope退出0。环境别名可取实际base_url去除userinfo后的origin之SHA256，保持所有run一致，不输出凭据。初始化现有历史残留是接入前置：先准确ID seed，再在新journal/runner路径核实写入拒绝、readonly放行；只有安装脚本而没有seed及pre-dispatch接入，不视为门禁生效。

## 按进展修复与失败确认

失败清理核验后，record以下一种事件才重新进入pending：

```json
{"type":"repair","event_id":"repair-A-1","case_id":"A","root_cause_id":"locator-1","evidence":["test/.cache/diagnosis.json"]}
```

- `repair`：附明确诊断和修复证据；获准范围内的测试代码、fixture、runner及报告绑定问题持续修到可用，不限制每case累计次数。先验证受影响目标，稳定后完成要求的回归；不改业务预期或吞异常制造通过
- `confirm`：相同结构，type改为confirm；每次有新的诊断依据或相关环境恢复证据才确认，保留原失败。不按case历史次数阻止后续确认；网络确认前先核环境和unknown创建
- 同根因、同输入和环境且没有新证据时停止重复执行，改查页面、日志、网络或依赖。程序仍要求终态、真实证据文件和清理核验；证据是否带来进展由runner/Agent判断，不用更换event_id或root_cause_id代替诊断
- 失败优先按[代码与部署版本分流](reports.md#失败先核代码与部署版本)定点核查可读的前后端实现。证据充分已确诊的产品bug保留失败并登记分诊，不为凑确认次数record `repair/confirm`或重复领取；其他独立目标继续。当前源码已修复不等于环境已部署，只有相关恢复/部署证据或明确待验证假设才定向复测，不伪造环境变化解除block
- 输入变化只失效依赖该输入的通过观察。失败仍需显式诊断，修复次数仅记历史；无需迁移已有journal。真正缺权限、预期、环境或资源清理依据时，保留对应工作包，不因累计次数放弃可修复用例
- 一个连续区间由runner完成，不逐case返回Agent。未知根因、修复和状态对账交回主Agent自主处理，完成验证后继续派发；只有确实需要用户决定或无法自行取得的恢复条件才转人工，不把交回Agent等同于暂停等用户

## 观察与正式验收分开

checkpoint始终输出`formal_regression_completeness=not_evaluated`。它解决续跑选择，不把日志观察升级为已绑定结果；现有Attempt审批、原生JSON/blob/HTML、输入指纹、full全部分片和资源门禁保持原契约。可报告增量执行进度与证据限制，但只有原门禁通过才能声称完整回归。旧的纯日志通过不补造为正式结果。不要为了报告收尾重复跑全量；确需当前代码完整验收时，先固定代码与环境，再一次执行合法的full/分片，不跨代码版本拼接

### 增量任务的完成标准

用户明确选择断点增量、不再全量时，以当前授权目标的执行和分诊完成为收尾标准，不把`regression_completeness=incomplete`单独当作继续派发理由。一次程序核对实际发现集合与逐case最新观察集合、证据可用性和清理义务；目标未变时复用发现，不每次汇报重做审计。汇总标明跨版本及受影响结果的失效范围，不能据此声称当前代码full或需求全覆盖。

- 已执行失败并已充分确诊的产品问题属于执行结论，不是未编用例；报告具体未到达分支，产品修复不自动纳入测试目标
- 未观察目标、脚本不可用、未绑定日志、资源未决及API归属分别保留；用户已确认的排除/暂缓不继续当作未解决输入反复追问
- 对仅有旧日志的目标，先查原生JSON、sidecar及可用中间产物。能合法绑定就复用；仍无法确定最终结果且目标仍在授权范围时，仅补跑受影响目标，不凭日志推断通过或重跑整批
- issue按具体缺陷及证据分别结案：同一case中的业务执行、编辑交互、清理和证据绑定可能是独立问题。后续通过只关闭实际覆盖且有对应证据的缺陷，不因主流程通过批量关闭未验证分面；独立补证充分时也不为改写旧native再跑整例。完成审计核对授权范围内的未编、未观察及未决义务，另列业务修复和环境部署状态，不将已有结论反复扩成新测试项目。
- 最终交付一份当前结果索引和剩余工作清单；后续更新同一入口，旧尝试保留在原路径，不逐批生成多套重复盘点和长期历史段落

### 跨run证据与回收

checkpoint复用的证据可能来自已结束run。回收器只识别其支持的配置、计划和状态引用，不会递归解释项目自定义latest union、任意JSON或HTML里的全部间接依赖；只保护索引文件不等于保护被引用的JSON/blob/HTML和附件。

在下一次`prepare-regression`之前，由项目adapter展开仍有效的原生attempt/merge目录，写入`test/spec.md`的workflow-config，例如在既有`retention`对象内设置`protected_paths`为具体的项目相对目录数组。现有回收器按配置中的路径保护，无需新建事件；列表不含凭据，不使用glob。无法完整枚举时设置`"retention":{"before_regression":false}`，继续执行测试，待整轮索引归档后再按显式清单回收。不要靠延迟回收天数推断证据已无依赖。

证据已被回收或意外缺失时，先查回收记录并保存缺失边界；摘要、SHA256或pointer不能还原原件。需要新的确定结论时仅补跑相应case，旧历史仍标原件已回收，不称已恢复。该保护策略适用于跨run断点，不要求所有普通回归永久保留全部历史。

## 生成剩余工作包

把已有证据归一化为`{"items":[...]}`，运行：

```sh
python3 "$SKILL/scripts/work_packages.py" --project "$PROJECT" \
  --input "$NORMALIZED" --output test/.cache/work-packages-v2.json
```

每项必填`case_id/category/evidence`，可带`root_cause_id/component/repair_attempts/confirmation_attempts/permission_required/cleanup_unknown/environment_changed`。category支持`product_candidate/needs_decision/environment/script/condition_missing/api_exclusive/deferred/unbound_observation/cleanup_unknown`。evidence是已有项目相对文件路径，复杂原ID或证据指针由项目adapter另存索引，不存凭据

仅明确根因按root_cause_id合并；component聚类标为`component_triage_only`，不推断共同bug。`constraints_by_case`保留其他门禁及历史次数。`repair_attempts/confirmation_attempts`可缺省，只用于记录，不产生次数门禁。输出不重试、不改历史、不解锁、不改变覆盖或验收；相同输出仅允许同内容幂等重放

### 输出action与runner路由

`category`是输入分类，枚举保持不变；以下是输出`packages[].action`。runner先检查顶层`schema_version`，当前为`2`；不支持的版本或未知action停止自动派发并报告，不回退到执行测试。权限和清理门禁优先于category路由。action是下一项工作建议，不生成额外授权，也不全部代表用户等待：`repair_then_affected`中的测试工具维护默认由主Agent处理，业务修改及部署仍需对应明确授权；`cleanup_review_no_blind_retry`和`review_environment_state`按原数据权限及证据核实；`awaiting_authorization`只用于核对既有授权后仍确实缺权限的情况，不为方便执行直接清除输入标志。取得新证据后保留旧包并重新归一化生成新包，门禁满足后续跑。

| 条件（按优先级） | 输出action | runner下一步 |
| --- | --- | --- |
| `permission_required:true` | `awaiting_authorization` | 等待相应授权 |
| `cleanup_unknown:true`或category为`cleanup_unknown` | `cleanup_review_no_blind_retry` | 核清资源和清理义务 |
| category为`script` | `repair_then_affected` | 自主诊断修复测试工具问题并验证受影响目标；不得据此修改业务代码 |
| category为`environment`，缺省`environment_changed` | `review_environment_state` | 状态未知；只读核实相关故障条件，不启动确认复跑 |
| category为`environment`，`environment_changed:false` | `awaiting_environment_change` | 已知故障条件未变；等待相关恢复证据 |
| category为`environment`，`environment_changed:true` | `diagnose_dependency_before_retry` | 依据变化证据核实当前依赖，满足原门禁后再决定是否确认 |
| category为`product_candidate` | `triage_product_no_automatic_fix` | 产品分诊，修改产品另依授权 |
| category为`needs_decision` | `diagnose_readonly` | 只读补证，澄清预期 |
| category为`condition_missing` | `supply_condition` | 补齐缺失条件 |
| category为`api_exclusive` | `handoff_api` | 转交API测试流程 |
| category为`deferred` | `preserve_deferred` | 保留暂缓状态 |
| category为`unbound_observation` | `preserve_evidence_limitation` | 保留观察及证据限制 |

环境状态由项目adapter/runner依据`evidence`中的当前诊断填写。`environment_changed`针对相关故障条件，不能用无关部署变化或累计确认次数代填；缺省表示未知，显式值必须为boolean，不能传null、字符串或0/1。核实后用更新的归一化输入生成新的工作包文件，不覆盖旧包；工作包本身不执行健康检查，也不解除checkpoint中的依赖block。`true`不代表环境已健康或自动获得重跑授权。

### 旧action迁移

移除累计修复/确认次数上限后，**`repair_budget_exhausted`、`review_repair_budget`、`review_confirmation_budget`已废弃，当前工具不再生成**。这些名称是旧输出action，不是category；不要加入输入category枚举。

| 旧action | runner迁移方式 |
| --- | --- |
| `repair_budget_exhausted`、`review_repair_budget` | 删除次数耗尽/补齐预算的路由；重新归一化输入生成v2包。无其他门禁的script项现在统一为`repair_then_affected` |
| `review_confirmation_budget` | 不再要求补齐确认次数；核实相关环境状态，按上表三种状态路由 |
| `awaiting_environment_change` | 名称保留，但不再由`confirmation_attempts >= 1`推断；v2仅在显式`environment_changed:false`时生成 |
| `diagnose_dependency_before_retry` | 名称保留，v2仅在显式`environment_changed:true`时生成；缺省状态改为`review_environment_state` |

输入字段及checkpoint journal无需迁移，旧次数仍保留为历史记录。消费工作包的runner须显式支持v2路由；已有v1包不改写结论、不直接作为v2执行队列，保留原文件并从当前证据生成新包。不要仅对旧action做字符串替换，因为授权、清理和环境状态都需要重新核实。

正常只输出ready/blocked/复用数量和工作包摘要；细节留文件，全部执行结束后一次合并和报告。新报告失败只修绑定/产物，不重复业务执行
