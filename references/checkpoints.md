# 断点调度与有限工作包

用于长回归、中断续跑和失败确认。程序负责日志、输入指纹、领取、修复历史和分组；Agent只诊断未知根因、审核依赖闭包及编写修复。以下入口独立于`workflow.py`，不启动产品测试、不修改验收状态、不授予权限、不替代浏览器配额

## 一次接入项目runner

项目runner在真实发现后生成精确清单，接入一次即可复用。默认保留原生JSON/blob/HTML reporter，并追加`scripts/checkpoint_playwright_reporter.mjs`作为观察sidecar。自动retry和repeatEach关闭；每次确认或修复复测领取独立attempt

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

每个onTestEnd写入并fsync，不保存原始错误或请求。sidecar要求实际`result.retry=0`；非0视为runner配置错误，写入mapping_error并阻止整批摄取，不将重试通过覆盖旧失败。retry/repeatEach由项目runner执行前关闭，checkpoint init只校验manifest，不修改Playwright配置。完整映射冲突和未知目标同样使sidecar失败；原生reporter仍保留实际结果。一个attempt每case只允许一个结果

## 中断恢复与依赖熔断

先查真实进程。仍在运行则接续等待，不重启。进程已退出时先保留原生文件、sidecar和真实退出回执，再摄取完整观察：

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

## 生成剩余工作包

把已有证据归一化为`{"items":[...]}`，运行：

```sh
python3 "$SKILL/scripts/work_packages.py" --project "$PROJECT" \
  --input "$NORMALIZED" --output test/.cache/work-packages-v2.json
```

每项必填`case_id/category/evidence`，可带`root_cause_id/component/repair_attempts/confirmation_attempts/permission_required/cleanup_unknown/environment_changed`。category支持`product_candidate/needs_decision/environment/script/condition_missing/api_exclusive/deferred/unbound_observation/cleanup_unknown`。evidence是已有项目相对文件路径，复杂原ID或证据指针由项目adapter另存索引，不存凭据

仅明确根因按root_cause_id合并；component聚类标为`component_triage_only`，不推断共同bug。`constraints_by_case`保留其他门禁及历史次数。`repair_attempts/confirmation_attempts`可缺省，只用于记录，不产生次数门禁。输出不重试、不改历史、不解锁、不改变覆盖或验收；相同输出仅允许同内容幂等重放

### 输出action与runner路由

`category`是输入分类，枚举保持不变；以下是输出`packages[].action`。runner先检查顶层`schema_version`，当前为`2`；不支持的版本或未知action停止自动派发并报告，不回退到执行测试。权限和清理门禁优先于category路由。action是下一项工作，不全部代表用户等待：`repair_then_affected`、`cleanup_review_no_blind_retry`和`review_environment_state`交主Agent自行修复/只读核实；`awaiting_authorization`只用于核对既有授权后仍确实缺权限的情况，不为方便执行直接清除输入标志。取得新证据后保留旧包并重新归一化生成新包，门禁满足后续跑。

| 条件（按优先级） | 输出action | runner下一步 |
| --- | --- | --- |
| `permission_required:true` | `awaiting_authorization` | 等待相应授权 |
| `cleanup_unknown:true`或category为`cleanup_unknown` | `cleanup_review_no_blind_retry` | 核清资源和清理义务 |
| category为`script` | `repair_then_affected` | 诊断并修复已授权的测试问题，验证受影响目标 |
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
