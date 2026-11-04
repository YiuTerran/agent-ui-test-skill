# 测试套件与共享用例

一个技能维护共同生命周期，UI/API分别按需加载专项指引。一个工程的工作流对应一个稳定suite；前端、后端各自保留测试、配置、run、Attempt、报告和执行结果，不联合启动或互相解释执行产物。

## 新suite

```sh
python3 "$SKILL_DIR/scripts/workflow.py" init --project "$PROJECT" --kind api --suite-id backend
```

UI使用`--kind ui`和自己的稳定suite ID。新suite要求原工作流资产不存在；不覆盖已有配置。旧的无类型`init`仍保留版本1初始化行为，便于既有接入兼容；本技能新接入明确传类型。

配置仍使用版本1的workflow-config，在其中增加：

```json
{
  "suite": {"id": "backend", "kind": "api"},
  "catalog": {"root": "../frontend"}
}
```

root是显式选择的目录，目录内使用`test/plans/catalog.json`保存版本2业务资产；默认root为`.`，也可选独立目录。相对root以当前工程解析，解析后固定到该根目录；业务计划/baseline路径相对catalog根目录，spec/file及产物路径相对本工程，不能用`../`绕出各自边界。公开配置优先用可迁移的相对路径，受限绝对路径使用项目已有本地配置机制，不提交私有路径。

加入已有catalog时，init保留catalog字节并从中带入scope/source候选；本工程环境、身份、命令及实现仍须核实。回执的catalog_asset列出目录、相对路径及是否新建；本工程内的catalog也计入created/preserved，外部catalog单列，避免被误当成本工程路径。创建空catalog后再写入当前获准需求与候选case，不把初始化当作审核或业务通过。

## 业务与实现格式

共享catalog的顶层为：

```json
{
  "schema_version": 2,
  "scopes": ["device"],
  "sources": {
    "S1": {"scope": "device", "kind": "requirements", "revision": "1", "fingerprint": "reviewed-content"}
  },
  "requirements": {
    "R1": {"scope": "device", "source_id": "S1", "expected_version": "1", "review": "approved", "case_ids": ["C1"]}
  },
  "cases": {
    "C1": {
      "scope": "device", "source_id": "S1", "requirement_ids": ["R1"],
      "title": "无权限用户不能删除设备", "expected": "请求被拒绝且设备未改变",
      "plan": "test/plans/C1.md", "review": "approved", "required_kinds": ["ui", "api"]
    }
  }
}
```

示例中的approved与版本仅说明格式；实际登记必须依据本轮真实来源和审核。代码派生来源用`kind=backend_code`，必有location及revision/fingerprint；需求仍是业务预期依据，有冲突需澄清。

本工程`test/plans/coverage.json`只保存版本2实现：

```json
{
  "schema_version": 2,
  "scopes": ["device"],
  "implementations": {
    "backend-C1": {
      "case_id": "C1", "suite_id": "backend", "kind": "api",
      "spec": "test/api/device.spec.ts", "test_id": "API-C1",
      "title": "API-C1 无权限删除被拒绝", "acceptance_version": "1",
      "execution": "automation", "implementation": "valid"
    }
  }
}
```

每个业务case在同一suite内最多一个未退役实现；同一case可由多个suite分别实现。独立业务断言场景另建case；project、retry、repeatEach产生的运行实例由原生报告区分，不能复制为业务case。test_id唯一性只在当前suite检查，不能靠标题子串或case ID猜测映射。

新增case默认required_kinds为本次任务类型，只有明确需要双层覆盖时才写两种类型。已有UI-only case要增加API覆盖，在原授权范围内把api加入required_kinds，再登记本地实现；不复制业务case。API-only场景不阻碍UI suite验收。声明的当前类型没有实现时显示缺口，不能沿用另一suite的通过或有效代码状态。

来源、需求及业务审核共享；实现资格、acceptance_version与结果分别维护。summary的coverage_layers区分business_cases、implementations及results_by_kind，其他类型为not_evaluated；当前suite验收不表示项目全部类型已通过，也不从历史报告拼造联合验收。

## 登记、并发与恢复

公共case/source/baseline事件保留原有来源核对与新增范围门禁。case事件的coverage是当前suite投影：业务字段写共享catalog，spec/test_id/title/acceptance_version/execution/implementation等写本地实现；business_title是业务标题，title是精确测试标题。case_id仍为业务ID，execution增加implementation_ids与suite并核对当前映射。

业务plan及baseline文件位于catalog根目录；来源快照用`source_cache capture --project CATALOG_ROOT`生成manifest。正常通过本工程preflight复用时，manifest及其files相对catalog根目录，observation.evidence相对本工程；直接调用`source_cache verify`时所有路径仍相对其`--project`。本suite的source事件与采集证据仍使用本工程路径。跨工程时分别保存本轮读取的本地证明与共享baseline，不能把另一工程的相对路径误当成本地证据。源文件和共享baseline须在所属仓库忽略策略下保存，不把受限正文提交为公共资产。

运行中冻结已选映射；跨suite业务预期变化使相关观察失效，本地测试标题/文件维护不会修改另一suite。共享目录使用catalog锁及提交前版本比较，冲突后重新读取并审核，不覆盖其他writer。读取旧格式无改写。

跨文件更新先写本工程`test/.cache/catalog-update.json`恢复记录，再提交catalog和本地coverage，最后标记完成；运行state仍是另一提交。中断时普通操作拒绝继续：

```sh
python3 "$SKILL_DIR/scripts/workflow.py" catalog-recover --project "$PROJECT"
```

该命令仅在文件仍匹配原值/拟提交值时补齐原提交；遇到更新后的冲突拒绝自动覆盖。随后显式resume并核对证据，不重跑业务来修复登记问题。catalog与业务计划应纳入适当版本控制，各根目录的`/test/.cache/`须忽略且未被跟踪。

## 旧UI项目迁移

版本1仍按默认UI读取；API和共享catalog要求版本2。迁移须没有未结束run和活跃调度配额，先预览后按同一参数执行：

```sh
python3 "$SKILL_DIR/scripts/workflow.py" migrate --project "$PROJECT" --suite-id frontend > migration-preview.json
python3 "$SKILL_DIR/scripts/workflow.py" migrate --project "$PROJECT" --suite-id frontend --apply --input migration-preview.json
```

预览文件必须与当前资产及参数完全一致；迁移保留ID、版本、审核、旧测试映射和历史run，不把旧UI重标为API。只支持原地迁移，业务文件位置不变；跨目录迁移需先审核并调整所属根目录下的文件与路径。迁移后新建测试轮次，旧run证据保持历史语义；历史run不能换suite恢复执行。中断迁移也用catalog-recover补齐，再重核配置。

## 资源协调

共享case不意味着账号/业务数据隔离。两工程实际共用环境和资源时，双方显式配置相同目录：

```json
{"coordination": {"root": "../shared-test-state"}}
```

目录须预先存在，`test/.cache/`须忽略；root与catalog可不同。共享配额、身份池和checkpoint残留索引在此目录，run/证据/报告仍留本工程。环境标识、池名、池上限与账号/组织/配置/业务资源conflict_keys必须一致；调度owner_pid须代表实际任务。

UI和API按test_kind分别计算Agent与playwright_workers配额，各自应用本工程concurrency配置；旧租约未写test_kind时归入UI。配额账本保留全局计数和active_by_kind分类计数，UI占满其worker或Agent上限时仍可派发有余量的API任务。API执行占自身playwright_workers，browser_parallel_cap可设0；UI保留worker与浏览器绑定，实际浏览器认证另以UI任务声明。整机可用CPU、内存、宿主槽位及pressure继续限制派发，内存估计包含两类活跃占用；类型独立不等于物理资源无限。CPU/内存未知时保守派发，worker内HTTP并发和服务限流仍由项目fixture控制。

所有writer在派发前执行共享残留gate；cleanup_state仍须真实回读证明，不以进程退出代替资源缺席。共享目录不可用、配置不一致或状态未知时停止相关派发，不临时退回各自账本。遗留本地账本已有活跃租约、池账号或残留时，先在原配置下核清后再启用共享协调，不能通过切root清空占用。

启用[定时资源控制](resource-control.md)后，UI/API共用同宿主controller及实测执行者容量，不再用上段固定每worker内存估计判断扩容；各suite显式并发/浏览器上限继续有效。资源预留只协调启动竞争，当前物理占用以实时采样为准；减员须完成当前case和cleanup，不提前释放租约。

首次状态操作绑定规范coordination根目录到本地`test/.cache/coordination-root.json`；之后修改root会被拒绝，即使新目录为空。未绑定但已有本地配额、身份池或残留账本时也拒绝直接切换。当前不提供跨根状态迁移命令，需在原配置下对账并经审核迁移；不能删除绑定或账本来解阻。外部coordination模式的回收保守保留本工程历史run产物，报告给出`external_coordination_preserves_runs`限制，以免删除共享池仍引用的本地证据；不会声称已实现跨工程证据垃圾回收。
