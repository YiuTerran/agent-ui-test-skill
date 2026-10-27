# 本地工作流CLI与JSON契约

按需查阅：[CLI](#cli)、[Git资产](#git资产)、[事件字段](#record事件)、[状态与统计](#状态与统计)、[辅助脚本](#提速辅助脚本)、[回收](#运行历史回收)、[平台与故障](#平台与故障边界)。最小顺序见[工作流](workflow.md#最小命令顺序)，报告绑定细节见[报告](reports.md)。

Python3.10+标准库脚本只管理本地状态/证据，不访问网络、启动Agent、执行产品测试或删除业务数据。除`--help`外输出JSON；成功exit0，输入/占用/门禁失败exit2并含`error`。输入不得含密钥、Cookie、token或原始认证响应；`authref`只存引用，字段过滤不能替代保密责任。

## CLI

入口：`python3 <skill>/scripts/workflow.py COMMAND --project PATH`。日常按里程碑批量登记，不按浏览器动作登记。

| 命令/参数 | 作用与门禁 |
| --- | --- |
| `init` | 原子补齐`test/spec.md`、`test/plans/coverage.json`，已有文件字节不变；返回created/preserved、缺项及发现的配置/脚本名称，不回显脚本正文，不推测授权 |
| `doctor` | 只读检查项目资产、配置/映射和`test/.cache/`的Git忽略规则；不补文件、不修改`.gitignore`或启动run |
| `preflight --owner NAME [--run RUN] [--input FILE]` | 本地诊断及一次start/renew，汇总已提供的真实来源观察、完整发现JSON与覆盖差集；不访问远端或执行测试，未提供输入保持unknown |
| `validate-input --run RUN --owner NAME --type TYPE --input FILE` | 无副作用重放record或collect-results门禁；TYPE=source/execution/record/report_bundle，不提交状态、coverage或创建锁 |
| `attempt-init --run RUN --owner NAME --bundle ID --input FILE` | 按真实计划创建新attempt与输出参数；无执行时间/退出码，不执行CLI，拒绝覆盖 |
| `attempt-bundle/attempt-check --run RUN --owner NAME --bundle ID [--input FILE]` | 核对真实command.json与JSON/HTML/blob；bundle排他生成事件供collect-results，check仅诊断，不登记执行 |
| `timing --run RUN [--input FILE]` | 只读区间及宿主计数汇总，不修改历史；未知保留null，重叠时间不重复相加 |
| `schema --type execution`或`schema --type report_bundle` | 从技能`assets/events.schema.json`输出实际输入形状，无需`--project`；不是输入验证器，不代替运行时来源/审批/证据门禁 |
| `start --owner NAME [--scopes a,b]` | 获取/恢复唯一兼容未完成run；多个匹配时显式选择。NAME为主任务独有且稳定标识，scope只能取固定范围子集 |
| `start --resume RUN [--reclaim --reason TEXT]` | 中断恢复；其他owner占用只有过期且显式reclaim才可接管，活跃租约拒绝。已release可接管，仍须恢复核对 |
| `start --new-round --owner NAME` | 与`--resume`互斥；同owner兼容旧run无running任务时结束旧轮、承接未决义务；其他owner须先显式接管 |
| `renew --run RUN --owner NAME` | 仅续当前有效租约；保留freshness_marker/source_checks。配置/coverage变化、旧格式无配置指纹、过期或终态须显式恢复 |
| `record --run RUN --owner NAME [--writer TASK_WRITER] --input FILE` | FILE为单事件或非空数组；整批验证后提交。委派writer只能更新预先分配给自己的task，不能扩大case/source或修改主run决策 |
| `prepare-regression --run RUN --owner NAME [--scopes a,b]` | 首次回归前一次安全plan+apply，保护当前run；不运行测试。后续复用，扩scope补预检；详见[回收](#运行历史回收) |
| `collect-results --run RUN --owner NAME --input FILE` | FILE为一个report_bundle事件；主owner原子登记bundle与精确映射的execution。失败state不变，原文件保留；不自动批准、清理或验收 |
| `status --run RUN [--brief或--full]` | 读取本地状态、推导阻塞/后续动作，不读取远端来源或通知用户；默认完整以兼容既有调用 |
| `validate [--run RUN]` | 检查资产结构、路径、指纹和已提供证据；有run则重核绑定。`valid`不证明业务覆盖或验收；无run返回missing_configuration |
| `report --run RUN --owner NAME` | 完整重核原始结果、逐次绑定、blob和原生合并，生成`summary.json`、`report.md`、未通过优先的`report.html`；差异exit2且保留原因，不执行merge或产品测试 |
| `release --run RUN --owner NAME` | 只释放占用，保留可恢复状态，不等于验收或清理完成 |

`record`/`collect-results`默认简报，`--brief`可显式指定；包含数量、相关ID及原因，后者还含`collection`的bundle_id、committed、complete、events_recorded、counts、unresolved。`complete=false`仍exit0，表示映射待核实，不代表通过。`--full`输出完整状态及regression_inventory；显式`status`仍默认完整。

`--brief/--full`仅适用于status、record、collect-results；其他命令携带这些参数时直接exit2，不修改状态，不静默忽略。report/validate保持完整证据检查和输出。

新增辅助入口的输入、输出及边界见[本地辅助入口](local-tools.md)。preflight的valid只表示本地预检完成，发现完整性由执行者声明；validate-input的valid不表示业务通过，bundle映射待核实仍须看collection.complete/unresolved。正常固定流程不额外调用validate-input/attempt-check。

schema_version=2的Attempt计划记录regression_mode=affected/full、可选完整发现指纹及明确shard。新run每个bundle绑定attempt_plan路径及attempt_plan_sha256，登记、结果导入和报告重核同时校验计划命令、单次blob及输入身份。summary.regression_completeness独立表示目标多重集合和全部分片是否齐全；新run的fully_accepted要求complete，业务通过另行核验；缺少bundle或full时为incomplete，not_declared只保留给旧run的未声明兼容记录。字段和收尾差集见[Attempt契约](local-tools.md#attempt准备与事件生成)。

简报含run_id/freshness_marker/claim/acceptance、counts（selected_cases/ready/blocked/work_needed/executions）、blocked_cases/regression_ready/work_needed/work_needed_reasons/next_steps、missing_configuration及configuration_drift/coverage_asset_drift。不扫描历史run：省略`global_residual_runs`并置`global_residuals_checked=false`，不把未检查当作零残留。完整status/report及最终验收继续做跨run残留核对和既有安全检查；简报只减少重复输出/扫描，不放宽提交、绑定或验收门禁。无需为每次简报追加完整查询，只有诊断或最终安全核对需要时才展开。

work_needed_reasons可直接定位review_not_approved、implementation_not_valid、acceptance_version_mismatch:R1、requirement_review_not_approved:R1、duplicate_test_id:C1/C2、missing_plan_file:path或missing_spec_file:path，按实际原因修复，不为取得完整状态重跑测试。doctor无写入（包括不获取会创建目录的项目锁），聚合缺失/解析/资产/配置/Git忽略错误，存在错误exit2。

覆盖资格、文件存在性及来源影响只在同一状态视图/连续execution批次中复用；非execution事件清除批次缓存，恢复重新计算。性能优化不缓存跨轮批准或跳过原始证据核验，不承诺固定耗时/倍数。

### 租约、预检与恢复

- `record/report/release/renew`等写操作要求当前owner及有效占用。新run默认3600秒，写状态续租；旧run沿用已登记租期，缺省300秒，不因升级延长旧占用。预计长操作超过剩余租期才renew；真正中断用start --resume、最新来源观察及recovery_review。
- run存于`test/.cache/runs/<UTC时间>-<随机后缀>/`。恢复必须匹配本轮scope子集/固定范围；与新subset重叠的未结束run须先继续或明确结束为partially_blocked，不能绕过旧待决项。结束run可显式resume调查，再回归用new-round。
- 新轮从各模块最近结束run承接未完任务、人审、issue、失效、半成品case及残留资源；标记inherited_from/inherited_runs/origin_run，ID冲突用`原runID::原ID`。不继承旧execution、usage、bundle或绑定错误；资源保留created_by_run和准确record_id。
- `prepare-regression`同轮freshness_marker、配置和scope有效时返回`preflight_reused=true`，不重新扫描/累计文件或时间。扩scope重建扩展计划，可回收原受保护的混合模块bundle；恢复/配置变化使预检失效，代码coverage维护不重复报告清理。retention.before_regression=false只记录明确配置跳过。
- 预检提交在锁内重读最新状态，合并cleanup_batches与已检查scopes；plan_scopes单独记录实际删除计划范围（旧格式回退scopes），缓存复用依据它。并发a-only/b-only不能证明a+b混合bundle已清，下次全scope预检仍需联合扫描，不重复累计旧批次。

## Git资产

`test/spec.md`中唯一的`json workflow-config`围栏块为配置来源；初始值见[模板](../assets/spec.template.md)。

| 字段 | 配置含义/默认值 |
| --- | --- |
| `schema_version`、`scope` | 版本1；scope.fixed固定范围，modules记录模块，CLI只选子集 |
| `source` | 数组`{id,scope,location}`，多模块用scopes数组；location为路径或来源标识 |
| `env`、`authref`、`data-policy`、`commands` | 初始null/null/null/{}，须从真实环境、授权和项目命令确认 |
| `concurrency` | max_parallel_agents=auto，auto_parallel_cap/browser_parallel_cap/playwright_workers均4；预算不等于授权 |
| `wait` | timeout_seconds=300，poll_seconds=15 |
| `regression`、`retention` | final=all_existing_automation；before_regression=true、reports=summary_and_unresolved_evidence。旧配置缺项按默认读，显式选择不重写 |

init不修改`.gitignore`；写入来源、认证或run证据前，writer必须核实`test/.cache/`被Git忽略，缺兼容规则时补`/test/.cache/`。既有spec没有配置块时按init的spec.workflow-config缺项在原文件补齐。

coverage顶层：`{"schema_version":1,"scopes":[],"sources":{},"requirements":{},"cases":{}}`。scopes与scope.fixed相同，ID稳定，不按标题再生成。

| 对象 | 必填/有效覆盖所需字段 | 可选字段、取值与门禁 |
| --- | --- | --- |
| `sources[id]` | scope（或scopes）、revision/fingerprint作为已审核有效版本 | revision/fingerprint/baseline_path可为null；只有批准的baseline事件升级，观察值留在run |
| `requirements[id]` | scope、source_id、expected_version、case_ids | status=`active`（缺省）或`retired`；**review可选，缺省`approved`**，显式`pending`/`rejected`使关联自动化case不算有效代码覆盖、不能进入合格回归/完全验收；不是source版本批准的替代品 |
| `cases[id]` | scope、source_id、requirement_ids、plan、spec、test_id、title、acceptance_version、execution、review、implementation | execution=`automation/manual/hardware/deferred`；review=`pending/approved/rejected`；implementation=`missing/draft/valid/retired`；legacy_group可标记旧组合测试 |

有效自动化覆盖必须同时满足：case.review=approved、implementation=valid；全部映射需求review=approved（含缺省）、acceptance_version匹配其expected_version；有效来源版本存在；plan/spec文件存在；test_id唯一且title完整；非legacy_group。manual/hardware/deferred单列处置，不能充当有效code覆盖；覆盖与上轮pass独立。

test_id唯一性检查只针对未退役且非legacy_group的case。多个legacy case可共用旧组合测试的test_id，不因此阻止validate或coverage更新；仍逐case列legacy_group_requires_independent_test，不计独立覆盖。组合执行需legacy_group=true及逐case的steps证据。

活动需求与未退役case须双向映射且scope/source一致。退役case留在登记/报告中，可从活动需求移除；最后一个case退役须同时退役需求或提供替代映射。legacy组合只有逐case的steps.result及本地证据才能传播逐例结果，不计独立覆盖。

每轮真实发现与需求/计划/coverage做完整差集，按模块汇总缺口后由用户选择新增范围，见[工作流](workflow.md#覆盖缺口门禁与模块补全)。all_existing_automation规定收尾执行集合；补写缺口以本轮用户选择为准。status/validate不证明语义完整。

## record事件

表中“必填”指事件必需字段；“条件”表示该分支必须提供，“可选”注明默认值。所有事件都含`type`；可带说明性字段，但不得含凭据。`at`可选，默认当前UTC，仅可用带时区ISO8601登记真实过去时间；seq由脚本生成。路径为项目内相对路径；evidence为文件路径数组，提供的文件必须存在。

### source、recovery_review

| 事件 | 必填字段 | 条件/可选字段与值 | 门禁 |
| --- | --- | --- | --- |
| `source` | source_id、freshness_marker、status | status=`changed/unchanged/unavailable`；observed_revision、observed_fingerprint；changed必填business_change布尔值；baseline_available、cache_hit可选 | marker取当前start；可读来源必须有evidence。unchanged须匹配有效revision/fingerprint；unavailable两观察值均null，不能解释为未变；changed须有观察版本/摘要 |
| `source`增删量 | 同上 | requirement_delta={added,updated,retired}，均非负整数 | 仅baseline_available=true且有效旧baseline_path文件存在时可记；旧正文缺失不推测增删量。business_change=false也不自动升级baseline |
| `recovery_review` | environment_checked=true、artifacts_checked=true | invalidate_case_ids、evidence可选 | 完成本次恢复核对；只失效指定case，不复用已失效pass |

### automation_expansion、task、case、baseline

| 事件 | 必填字段 | 条件/可选字段与值 | 门禁 |
| --- | --- | --- | --- |
| `automation_expansion` | owner、mode、scopes、gap_case_ids、user_reply、evidence | mode=`modules/all/skip`；scopes为用户选择新增的模块，all取本run全部scope，skip取[]；gap_case_ids为提交给用户的完整缺口清单，含未选模块和待建case的候选ID | 仅主Agent依据真实用户答复登记，evidence包含模块清单和答复；modules仅授权所选scope内列出的ID，all授权完整清单，skip保留缺口并回归已有。选择仅属当前run，新轮不继承 |
| `task` | task_id、phase、status、owner | phase=`impact/clarify/case_review/exploration/code/execution/acceptance`；status=`pending/running/blocked/approved/complete`；case_ids、source_id、affected_case_ids/affected_requirement_ids、evidence可选 | 同task不能改phase；来源审批绑定当时观察版本。需求变化须完成impact/case_review；需要clarify时显式建任务。未完成旧任务恢复后仍阻塞关联case |
| `task.dispositions` | 非自动化case_review时逐case提供mode、status=approved、acceptance_version、reason、evidence | mode=`manual/hardware/deferred` | case必须在task.case_ids中，mode和版本匹配登记case；有明确理由和已有证据 |
| `case` | case_id、action | action=`added/updated/retired/unchanged/needs_decision`；exploration/code=`none/pending/running/complete/blocked`（缺省none）；exploration_action=`executed/reused/not_needed/blocked`（缺省not_needed）；code_action=`added/modified/reused/retired/missing`（缺省missing）；coverage、requirements、evidence可选 | 新case需完整coverage；带coverage/requirements或退役须本轮可读来源、变化影响/审批完成，无对应人工阻塞。两映射更新保持Git资产约束 |
| `baseline` | source_id、approved=true、revision、fingerprint | baseline_path可选，有值须为已有正文文件 | 必须匹配本轮可读观察、impact/case_review完成且无对应待决人工事项；只该事件升级有效版本 |

- impact完成时可精确列受影响case或需求；未列则按该来源本轮全部case失效，未受影响case可继续旧基线回归。
- case保留最新状态和完整动作历史；本轮added后updated/unchanged仍只计一次added，updated后unchanged仍计一次updated。状态与报告动作分别登记。
- 新增自动化case/code及缺口case的探索/代码推进须匹配automation_expansion的模块和case清单；缺口来自接管时的代码资格及preflight真实发现。脚本拒绝未获选范围的新增回执，已有合格测试的执行不依赖新增选择。仅登记候选/待决事项保持待补；实际文件编辑与浏览器启动前由执行者落实同一门禁。summary/status及Markdown保留选择和未选/待选择的缺口，完整需求验收继续检查真实覆盖。
- 带coverage/requirements时先原子写coverage再写state，更新预期指纹；这是跨文件提交，不是数据库事务，中断后须恢复核对。

### resource、human、delegation

| 事件 | 必填字段 | 条件/可选字段与值 | 门禁 |
| --- | --- | --- | --- |
| `resource` | resource_id、status、created_by_run | status=`intent/confirmed/ambiguous/absence_verified`；kind、record_id、identifying_fields、evidence可选；confirmed必填owned=true、receipt | 初建只能intent；resource_id是本地意图ID，服务端准确record_id可确认后补。只建议清理所有权证明完整的confirmed；ambiguous先核实，前缀不证明所有权 |
| `human` | request_id、status；初建mode | status=`pending_notification/waiting/blocked/approved/rejected/resolved`；mode=`explicit_decision/observable`，后续不可改；case_ids、source_id、evidence可选；timeout_seconds缺省配置上限300 | 主Agent通知/登记；超时推导blocked，无后台轮询。解除须已有请求；observable用resolved+evidence，explicit_decision须approved/rejected，后者仍阻塞，时间/页面可用不能代替审批 |
| `delegation` | status、reason | status=`unavailable/available`；case_ids可选 | unavailable阻塞对应工作，不声称已委派；available解除相同case集合的委派阻塞 |

资源转换仅允许intent→confirmed/ambiguous/absence_verified、ambiguous→confirmed/absence_verified、confirmed→absence_verified；absence_verified必有证据。created_by_run为本run或显式继承资源的原run。脚本不删除业务资源，执行者按准确ID逆依赖清理并验证absence。

human范围规则（统一用于status、case更新、baseline）：

- case_ids非空时只阻塞这些case，source_id仅上下文；无case_ids才按source_id，无两者的新请求作用于本run。
- 更新未带范围字段则保留；带任一case_ids/source_id时两者整体替换，未提供字段清空。已有请求不能用空范围扩成run范围，要新request_id；保留case范围时同时提交case_ids。
- baseline只检查该来源impact确定的受影响case，尚无影响范围时检查该来源本轮case。

### execution

| 字段 | 必填性 | 取值/默认 | 门禁 |
| --- | --- | --- | --- |
| execution_id、test_id | 必填 | 稳定test标识、唯一执行ID | 重跑换ID；业务test_id匹配已审映射 |
| setup、attempt、result | 必填 | setup布尔值；attempt正整数；result=`pass/fail/skip/notrun` | 同test/setup重跑attempt递增；Setup单列，不计业务覆盖/通过数 |
| started_at、ended_at | 必填 | 带时区ISO8601，end≥start | 缺失明确报错，不补造时间 |
| case_ids | 业务必填 | 默认一个case；Setup可空 | 多case仅legacy_group=true，逐step证据才传播结果 |
| method | 可选 | 缺省automation，或manual/hardware | 与已批准执行方式匹配；自动化须当前有效代码 |
| bundle_id、runner_test_pointer、runner_result_index | 新CLI绑定必填 | 原JSON的tests对象指针及results索引；无result的原始skipped索引null | bundle先登记；同slot只绑定一次，全部Setup、业务、retry/未执行都保留 |
| steps | legacy条件 | `{caseID:{result,evidence}}` | 关联case必须在case_ids中，每step有本地证据；无证据不传播pass |
| runner_result、blocked_by、reason | 依赖阻塞条件 | notrun + runner_result=skip + 失败Setup execution_id + 具体原因 | 依赖须有实际证据；显式业务skip仍是skip |
| evidence、failure_details | 可选 | detail有值时必含expected/actual/phase/classification/location/cleanup；readback、evidence可选 | classification=`product_candidate/script/environment/needs_decision/deferred`；[报告示例](reports.md#一份可用的人类报告) |

业务执行须本轮来源已检查、恢复已复核，无对应任务/人工/失效阻塞；unavailable仅允许有有效旧baseline的stable回归，不能完全验收。缺执行为notrun；fail/skip/notrun如实保留，旧版本或失效pass不能验收。

Schema已编码blocked_by出现时的notrun/runner_result=skip/reason条件，以及failure_details出现时的6个非空必填字段（空对象/null不接受）。blocked_by指向的ID是否为本run失败Setup须运行时解析，独立事件Schema不能证明跨事件引用。

下例只说明字段；实际由collect-results生成。指针、时间和路径须取真实结果，不能照抄为执行证明：

```json
{
  "type": "execution", "execution_id": "E1", "test_id": "C1",
  "case_ids": ["C1"], "setup": false, "method": "automation",
  "attempt": 1, "result": "fail",
  "started_at": "2026-10-27T01:00:00Z", "ended_at": "2026-10-27T01:00:05Z",
  "bundle_id": "cli-01", "runner_test_pointer": "/suites/0/specs/0/tests/0",
  "runner_result_index": 0,
  "evidence": ["test/.cache/runs/RUN/attempts/cli-01/results/result.json"]
}
```

### issue、usage、phase_time

| 事件 | 必填字段 | 条件/可选字段与值 | 门禁/统计 |
| --- | --- | --- | --- |
| `issue` | issue_id、status、kind | status=`open/closed`；kind=`product_candidate/script/environment/needs_decision/deferred`；affected_case_ids、evidence可选 | 同ID合并case集合，按ID去重；product_candidate单计，不把环境/脚本/待决算产品bug |
| `usage` | measurement_id、source=host、metrics、aggregation | metrics的input_tokens/output_tokens/total_tokens均为真实非负整数或null；aggregation=`exclusive/inclusive`；coverage缺省partial，或complete_run；includes、evidence可选 | 只收宿主实测；子项先登记，inclusive不重复累计子项，拒绝循环/重复/重叠包含 |
| `phase_time` | phase、started_at、ended_at | task_id、owner可选；end≥start | 阶段按区间并集，保留原记录；跨阶段并行不相加当墙钟 |

usage仅唯一根项明确coverage=complete_run且有coverage_description说明全部执行者/阶段范围时给整轮totals；其他根项、覆盖关系或指标缺失则整轮总量null，partial根项去重小计放recorded_subtotals。主Agent须核对声明与真实宿主范围，脚本不能发现漏报。人等按human事件区间并集，裁剪至run开始后。

### report_bundle、merged_report、acceptance

| 事件 | 必填字段 | 条件/可选字段与值 | 门禁 |
| --- | --- | --- | --- |
| `report_bundle` | bundle对象 | 完整字段表与示例见[报告绑定](reports.md#绑定与校验)；bundle_id在run内唯一、scope属于本run | 文件位于本次attempt；登记保存SHA-256，后续逐slot绑定；兼容旧回执不等于新执行可不绑定 |
| `merged_report` | report.html_report、bundle_ids、command、started_at、ended_at、exit_code | 主owner登记实际离线merge，exit_code=0；输入为所有bundle的blob_reports | bundle_ids恰好覆盖本轮全部CLI；比较原始JSON全部尝试与合并HTML/附件，保存输入ZIP及输出摘要；新增CLI须重新合并 |
| `acceptance` | status、owner | status=`in_progress/blocked/partially_accepted/fully_accepted/completed_with_failures/partially_blocked/aborted`；reason、evidence可选 | owner必须主owner；终态为fully_accepted/completed_with_failures/partially_blocked/aborted；资源残留阻止fully_accepted |

新run固定`native_merge_required=true`，每个bundle必须绑定当前schema的Attempt计划，每次CLI必须JSON+非空blob_reports，正常同时HTML；最终必须登记并核验覆盖全部CLI的原生合并。旧run缺标志按false兼容，缺blob/JSON-only只作历史限制，不能用于新run或冒称原生合并已完成。

fully_accepted还必须同时满足：

- 本轮来源均已检查且可读；配置/coverage有效，scope具备非空已审核需求清单，需求完整映射到case。
- 无本run或其他历史run未核清资源，无未完任务、人工、失效或缺代码；完整检查不因简报省略。
- 全部自动化case有当前版本有效逐例pass；manual/hardware/deferred均有带版本、理由和证据的批准dispositions，manual/hardware还须当前逐例pass。
- 新run具有绑定完整发现清单的full回归，regression_completeness=complete且整轮绑定checked、原生合并核验通过；没有bundle或只有旧式直接execution时同样执行该门禁。旧run已有bundle时要求整轮绑定checked。
- 事件只记录主Agent判断，不证明业务语义全覆盖。产品失败可按completed_with_failures结束，不篡改历史结果。

## 状态与统计

完整status包含planning_needed_scopes、unmapped_requirements、claim、source_checks、residual_resources、global_residual_runs、pending_humans、blocked_cases、regression_ready、regression_inventory、work_needed、nonautomated_cases、next_steps、requirements_updates_paused。brief返回重点数量/ID/原因及历史未检查标记，不展开完整清单。

- next_steps给本地建议：影响分析、澄清、审批、升级有效baseline、失效修复及未完探索/编码/执行/验收；只省略当前观察版本已完成阶段。blocked_cases保留逐case原因。
- unavailable暂停需求更新；changed+business_change暂停受影响case直到impact/case_review、必要人工决定及显式baseline完成。未知、缺旧baseline、映射缺口、待审不能借“未变”绕过；无关有效旧基线case可回归。
- global_residual_runs跨scope列其他run残留及owner_cleanup/owner_reconcile，不取得他人资源。继承账本按origin_resource_id/created_by_run去重，新absence_verified可核销旧条目；本地残留含准确record_id、回执、来源run及建议。
- 缓存丢失可按coverage/文件重建队列，不推测旧执行或usage。coverage_asset_drift仅指本地资产指纹不匹配。

summary按稳定caseID列动作、逐次执行、最终有效结果、显式关联的资源与phase_records；只按case_ids或已登记task_id关联，不按共享任务猜逐例耗时，未关联资源保留全局台账。source_observations及真实requirement_delta保留，不造历史增删量。

counts区分issues_by_kind/product_candidates、setup_results/setup_executions、planned_case_actions/exploration_actions/code_actions、business_executions、independent_business_tests、execution_results/case_results、issues/resources；business_actual_attempts排除skip/notrun，dependency_blocked只计有失败Setup引用的notrun。计时/覆盖/报告口径见[报告](reports.md#历史报告与回收)，未知为null。

report返回summary、report_bundles、可选merged_report、report_consistency和report_paths（human_report/machine_report/review_html/merged_html）；performance含source_cache_hits/source_observations/regression_preflight。Markdown保留逐case、Setup、issue、资源及证据；沿用现有计划路径，无IoT迁移。release返回run_id/released/acceptance。state保留claim、来源、恢复、任务/人工/资源、case动作、issue、执行/事件、usage/phase_times、失效、指纹、resume_history及可选bundle/继承信息。

## 提速辅助脚本

均为本地Python3.10+标准库辅助，不启动Agent或产品测试；日常不与workflow组合入口重复执行。

### 双预算与就绪任务

入口：`python3 <skill>/scripts/scheduling.py {budget,ready,acquire,release,reap} --project PATH --input REQUEST.json`。项目所有run/CLI共享`test/.cache/scheduling`锁定账本。

| 输入/结果 | 字段与约束 |
| --- | --- |
| resources | available_cpus、available_memory_bytes、available_agent_slots、independent_tasks、pressure，未知null；尽力探测CPU/内存，Agent空槽由宿主提供 |
| tasks[task_id] | phase/status/owner/dependencies/writable_paths/conflict_keys/case_ids/isolation；readable_paths可选，缺省[]；实际传递fixture/helper/配置/认证输入由执行者声明 |
| isolation | 核实隔离用verified；无写集也填[]。未知只在自己isolation_group/case范围串行，无范围则保守串行 |
| quota | agents/exploration_browsers/playwright_workers；CLI workers必须等于实际--workers，与探索浏览器共享上限 |
| acquire | 原子重核预算/就绪；仅acquired=true可启动；false含配额/冲突原因，不先重复budget/ready |
| release/reap | release用lease_id/release_token，teardown后释放；reap须同宿主owner_pid确认死亡或规范workflow任务终态，不按超时猜回收 |

```json
{"run_id":"RUN","owner":"worker-a","owner_pid":12345,"task_id":"explore-a",
 "tasks":{"explore-a":{"phase":"exploration","status":"pending","owner":"worker-a",
 "dependencies":[],"writable_paths":["test/src/a.spec.ts"],
 "conflict_keys":["shared-record:fixture-a"],"case_ids":["C1"],"isolation":"verified",
 "quota":{"agents":1,"exploration_browsers":1,"playwright_workers":0}}}}
```

预算返回requested/effective/available、限制/事实来源及登记峰值。每浏览器约2GiB+2GiB余量是估计；未知CPU或内存只允许1个总浏览器，pressure=true暂停新增、不撤现租约；显式配置仍受已知资源约束。macOS有界vm_stat、Windows GetProcessAffinityMask/GlobalMemoryStatusEx只作探测，不冒充进程/内存实测峰值。

相同冲突键、相同/父子写集互斥；写集与他人读集重叠返回read_write_paths，读读共享。路径用项目相对字面安全路径及本机别名语义；同账号不一律串行，按组织切换/全局配置/共享数据建键。执行读集与探索写集冲突只串行相关任务，可用3个CLI worker+1个探索浏览器；依赖不挂无关模块。旧租约未声明读集须等结束或先核实冲突。PID须代表实际任务生命周期，不用已退出临时shell；配额不能调度Playwright内部test，冲突须落实到suite/project/批次。

### 来源快检缓存

入口：`python3 <skill>/scripts/source_cache.py {capture,verify} --project PATH --input REQUEST.json [--output NEW_FILE]`。

| 操作 | 输入 | 语义 |
| --- | --- | --- |
| capture | source_id/revision/fingerprint/fingerprint_basis/files | 完整本地文件SHA-256清单；basis只接受canonical_content/document_bytes，版本/标题/时间不足以证明未变 |
| verify | manifest、round_started_at、observation | round_started_at取state.created_at，恢复取最新resume_history.at；observation含source_id/revision/fingerprint/fingerprint_basis/readable/observed_at及本地evidence |

观察必须来自本轮真实读取且不在未来。身份/指纹及全部文件匹配返回action=reuse/cache_hit=true，变化/损坏为full_read，不可读为unavailable。仍须登记workflow source及cache_hit，不能用旧缓存断言远端未变。输出排他创建，不覆盖旧manifest。

### 运行历史回收

日常用prepare-regression；以下仅人工预览或核实输入变化后的恢复：

```sh
python3 <skill>/scripts/retention.py plan --project CANONICAL_PATH --scopes a,b --current-run RUN --output NEW_PLAN.json
python3 <skill>/scripts/retention.py apply --project CANONICAL_PATH --input NEW_PLAN.json
```

project用真实规范路径，导出计划放项目test目录外；内部注册在`test/.cache/retention/plans`。apply仅接受同一已注册不可变计划，重核摘要、目录身份、控制状态及引用；输入/quota变化使计划失效后先核实再重建。按workflow→quota固定顺序持锁。

| 规则 | 范围/结果 |
| --- | --- |
| 白名单 | 已结束非活跃run的attempts报告/blob/结果/附件、merged报告及不再需的sources/handoffs/discovery；Git资产不回收 |
| 始终保护 | 当前/活跃/恢复run、有效baseline、显式审批及未核清业务资源必要恢复路径；保留state.json/summary.json/report.md/report.html/command.json |
| 历史失败 | 产品/脚本/环境失败、未关issue、旧pending任务和绑定异常不单独保护大报告；保留原结论/小摘要，不要求先登记修复 |
| 混合模块 | bundle/merged全部scope符合才回收；通用目录说明或.gitignore输出不是保护引用 |
| 路径安全 | 路径逃逸、symlink、文件替换及Windows别名/句柄边界继续拒绝；无可靠归属/范围或白名单外局部保留，不用glob猜删 |

plan/apply的diagnostics包含candidate_files、selected_files、protected_files、zero_reason、protections、excluded_files_by_reason。candidate=selected+protected，不能重叠；protections含path/source_file/field/reason/files/sample_paths（最多3条），说明首个匹配保护及来源；excluded_files_by_reason说明候选前排除。

零回收可为no_eligible_history（无历史/全部活跃/无本轮产物）或protected_references，非零时zero_reason=null。仅候选去向无法解释或重叠报cleanup_candidate_mismatch、exit2并附样例，不把零回收一律判失败；其他安全错误照常拒绝。异常一次定位，输入未变不重复plan/apply、改工具或多轮安装；相关清理未完成不测试，独立范围可继续。

plan.cleanup_scan_seconds为扫描耗时；apply的timing.cleanup_scan_seconds保留原扫描，timing.cleanup_apply_seconds含复核、隔离和删除，不称纯unlink。同计划中断按manifest恢复；完成后幂等replayed=true返回首次计时，不重复累计，旧计划未计量项为null。

日志在`test/.cache/retention/manifests`，保留路径/SHA-256/字节/模块/原因/时间、原runner统计及limitations。旧异常/不完整登记不能阻断其他可证明安全的回收；意外缺失不伪造删除记录。控制状态不可读、路径风险或文件被替换仍拒绝。公开API：make_plan/apply_plan，is_recycled/recycled_bundle为只读，不表示重跑/通过。回收后的报告口径见[历史报告](reports.md#历史报告与回收)。

### 批量结果候选

只读预览/诊断/手工映射时才用：`python3 <skill>/scripts/import_results.py --project PATH --run RUN --bundle BID [--output NEW_FILE]`，API为import_results(project,run_id,bundle_id)。先登记真实bundle，从原JSON生成events/unresolved/counts，不写state；主Agent核实后record提交，输出不覆盖，重入不重复已有绑定。

精确核对文件、标题、project；不同describe同名spec为歧义，不当repeat。Setup、业务、retry/repeat均保留；无依赖证据的skip仍skip，legacy多case/模糊映射留unresolved，不猜notrun或case。正常使用collect-results，不再重复此分步路径。

## 平台与故障边界

占用是协作标识而非认证系统；writer只限制已登记委派task写入。状态操作排他锁、同目录临时文件刷盘+os.replace；首次排他创建，锁不是后台服务。单JSON原子替换，coverage/state跨文件崩溃须start恢复并recovery_review；init逐文件补缺，中断后可继续。文件存在/一致性不证明实际test声明、业务语义、远程创建/删除、人工身份或宿主测量，主Agent负责采集审核。

- **路径/编码**：UTF-8，证据用项目相对`/`路径；CLI可本机绝对路径，含空格加引号。Windows拒盘符相对、ADS、设备名、尾随点/空格；读写集按大小写/分隔符别名互斥。PowerShell用`$env:NAME = "value"`。
- **锁/进程**：macOS/Linux用fcntl.flock，Windows用msvcrt首字节锁、有界等待；只短时持锁，JSON另替换。Windows OpenProcess(SYNCHRONIZE)+零等待只读探测，无权/API不可用为unknown、不回收；不用os.kill。进程退出释放内核锁，不自动释放任务配额。
- **清理**：POSIX保留dir_fd/O_NOFOLLOW；Windows固定本地磁盘，从盘根逐级以目录句柄和单段名称打开，不重新解析绝对路径；拒reparse/junction和多硬链接，以已验证句柄枚举/读写/重命名/删除。目录禁止共享删除，允许内核重命名所需的共享写；即使校验后目录被设为reparse，句柄相对子项操作也不能重定向到外部目录。普通文件禁共享写删，锁文件可共享写。能力不足或占用失败保留恢复记录；UNC/网络盘不作目标，推荐NTFS。
- **持久化**：Windows不覆盖rename，不依赖硬链接/目录fsync；支持进程中断恢复，不承诺断电目录元数据持久性。同缓存不跨主机或WSL/原生Windows同时共享。

发布/维护时可在目标宿主运行`python3 -m unittest discover -s "<skill>/scripts/tests" -v`（Windows用`py -3`）；非Windows的skip不计Windows通过。mock仅证明调用约束，原生Windows测试才验证句柄/junction/进程/资源；缺symlink权限仅跳该项，junction继续。发布在Windows/Linux/macOS各自测；ego-lite/Playwright集成能力在实际目标环境另行验证，本地适配不证明浏览器可用。
