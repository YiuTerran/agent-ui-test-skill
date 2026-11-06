# 定时资源控制

动态资源控制覆盖Agent、Playwright case、独立sweep及fixture内可并行任务。先按依赖图识别独立分支，再在实际领取的容量内派发；只增加执行者数量不能证明内部串行瓶颈已经消除。主Agent负责协调，调度账本中的agents表示实际领取工作包的执行者，不包含仅协调的主Agent；使用sub时最低保留1主1sub，宿主没有子Agent能力时由主Agent继续。

同一执行阶段复用实测资源快照，为各层设置共享、有限的队列和容量，明确Agent、case与I/O预算的换算及总上限。Playwright worker上限、Attempt计划与配额一致；case池与内部请求池分层领取，内部I/O容量由整个阶段共享，避免每个case各自扩容后形成容量乘积。初始化在可靠健康采样和已预留上限内利用空闲资源，持续运行再按阈值扩缩；采样未知不放行新业务。没有独立任务、真实槽位或可靠采样时不扩容。

## case、sweep与内部任务

项目runner在实际派发位置接入动态准入，覆盖case准备至cleanup完成、轮末独立sweep，以及资源准备、查询回读和清理中的独立分支。资源创建遵从依赖图，清理遵从逆依赖；不同准确资源或互不依赖的事件码可并行，单个资源的归属核对、删除及缺席回读保留次序。并行分支发生失败时，等待全部已启动操作结束后才交回清理或释放预算，避免提前失败后仍有后台写入。

I/O令牌尽量在实际HTTP请求或协议发送等叶子操作领取，等待命令、断连及轮询间隔不长期占令牌；高层函数持有令牌后再调用同池叶子任务可能死锁。同步命令请求会等待设备回复时，为回复保留独立推进通道，该通道计入总预算，缩容排空期间仍可完成已在途握手。跨进程共享同一容量锁或队列，不能用每个worker的本地信号量冒充全局上限。

高压或采样未知时暂停新业务，已在途case及owned清理保留最低推进能力。sweep采用动态队列，受资源压力约束，业务派发阻断标记不阻止原账本恢复。每次清理恢复使用独立阶段记录，保留原采样历史；失效、停止或上下文不匹配的控制状态关闭新增，不能复用旧健康状态。收缩等待在途任务排空，不强杀或提前释放租约。

登录准入、账号隔离、共享配置读写锁和数据冲突属于独立约束，按冲突scope局部限流；整机空闲不代表认证服务或目标后端允许无限并发。报告说明实际接入的层次、预留与当前容量、调整原因和采样范围，并区分CLI墙钟、fixture收尾、sweep及登录等待。用同范围真实执行比较提速，离线并发测试只证明调度和安全约束。

## 项目接入任务池

`scripts/task_pool.py`把已领取阶段预算落实为跨进程任务槽和全阶段共享的叶子I/O槽；`assets/task-pool-client.mjs`提供Node/Playwright客户端、HTTP代理和等待全部分支结束的`settleAll`。脚本复用`resource_controller.py`的实测采样及平台锁，不调用产品API、不增加Agent、不领取或释放工作流租约。已有等价实现可继续使用，用相同行为契约核验即可。

修改runner时先定位实际派发点，再改以下层次，避免只调整Playwright的`workers`：

| 层次 | 接入位置与约束 |
| --- | --- |
| runner | 从已获配额计算任务上限，显式设置全阶段I/O上限和回复保留槽；启动monitor，向子进程传递同一pool路径、project、run和stage |
| case | 自动fixture在任何资源准备前领取任务槽，持有至该case全部cleanup结束；场景fixture依赖admission fixture，保证teardown次序 |
| fixture | 按依赖图并行独立分支，初始化Promise按准确scope复用；全部已启动分支settle后才进入清理 |
| 请求与协议 | HTTP代理覆盖测试直接请求及fixture请求；协议发送/回复接入`runIO`；等待事件、轮询间隔和配置锁在令牌之外 |
| 独立sweep | 每组原账本领取cleanup任务槽，独立资源分支并行，单资源按归属核对→删除→缺席回读执行 |
| 收尾 | 等case、I/O和清理任务全部结束后停止monitor，再按原流程采集、对账和释放实际租约 |

### runner启动

项目runner可以导入模块，在真实CLI及其teardown的整个生命周期持有monitor。以下变量从实际Attempt及已领取预算取得；`io_budget`独立声明，不能再乘以worker数量：

```python
from task_pool import Monitor, configuration

config = configuration(
    project, run, "cli-01",
    maximum_tasks=reserved_workers,
    maximum_io=io_budget,
    progress_slots=reserved_reply_slots,
)
with Monitor(attempt / "pools" / "cli-01", config) as monitor:
    execute_cli(pool_directory=monitor.directory, pool_config=config)
```

`task_pool.py`及其同目录依赖来自实际skill路径；runner可将该scripts目录加入Python模块查找路径，或用`importlib`加载。`execute_cli`表示项目现有执行函数，需要实际传递配置、绑定环境并等待退出，示例不是独立可执行runner。CLI worker数、Attempt声明及已领取任务容量保持一致。任务池只在上限内动态准入，不会修改运行中的Playwright worker配置；worker等待槽位时仍占项目实际配额。

同阶段的所有worker共用一个pool。CLI与sweep顺序执行时，为sweep新建`pools/sweep-0001`等独立目录，保留CLI及每次恢复的采样历史；若两阶段或多个子执行者重叠，共用一个池或事先划分互不超额的预算，不能各自复制整份上限。登录准入、配置冲突锁、父执行者draining检查及checkpoint残留gate继续由项目接入，任务池不替代这些门禁。

也可启动`task_pool.py watch`，从stdout读到`{"event":"monitor_ready",...}`后再派发；保持stdin打开，所有任务结束后关闭stdin并等待exit0。stdin结束只停止该monitor，不自动终止worker或清理产品数据：

```sh
python3 "$SKILL_DIR/scripts/task_pool.py" watch \
  --directory "$POOL" --project "$PROJECT" --run "$RUN" --stage cli-01 \
  --maximum-tasks "$RESERVED_WORKERS" --maximum-io "$IO_BUDGET" --progress-slots 1
```

### Node与Playwright接入

项目可导入或复制`assets/task-pool-client.mjs`到现有测试工具目录，通过显式参数调用；复制后记录来源，后续维护验证项目实际实现。客户端每个case或sweep组只启动一个持久Pythonhelper，不为每个HTTP请求启动进程：

```javascript
import { holdTask, budgetApi, settleAll } from "./task-pool-client.mjs"

const admission = await holdTask({
  python, poolScript, directory: poolDirectory, project, run, stage,
  cleanup: recoveringOwnedLedger,
})
try {
  const api = budgetApi(rawApi, admission)
  const [parent, identity] = await settleAll([
    prepareParent(api),
    prepareIsolatedIdentity(api),
  ])
  await prepareDependent(api, parent, identity)
  await executeBusinessAndCleanup(api, admission)
}
finally {
  await admission.release()
}
```

示例中的项目业务函数必须实现自己的`finally`清理，admission的`release`只归还本地容量。Playwright中把admission作为场景fixture的依赖；case失败也须等场景cleanup结束再释放。准入fixture单独设置与`waitSeconds`匹配的timeout，区分等待容量和业务请求超时；不能为调度等待统一抬高业务超时。`budgetApi`包装`fetch/get/post/put/patch/delete/head`，其他方法绑定原对象；自定义HTTP客户端和协议客户端需在各自叶子操作接入`admission.runIO(() => actualRequest())`。代理对象已经领取令牌时，不在外层再包同池令牌。

同步命令等待设备回复时，回复叶子调用`admission.runIO(() => actualReply(), true)`使用预留推进槽；普通请求不能借用该槽。保留槽包含在`maximum_io`内，可设置0用于没有此类依赖的工程；`maximum_io`必须至少比保留槽多1。`settleAll`返回结果或在全部结束后抛出含各分支失败的`AggregateError`；并行失败不允许提前释放资源或跳过尚在运行的分支。

需要在helper丢失后的隔离期间推进owned清理时，用`admission.runCleanupIO(() => rawApi.delete(ownedResourceUrl))`显式使用预留推进槽；这里调用未包装的原始客户端，避免嵌套领取。该入口只用于原账本的清理及必要核验，不能用于新增业务。`cleanup: true`仅允许领取清理任务槽，不会把普通HTTP代理自动转换为清理请求。需要此恢复能力的项目应预留至少1个推进槽；未预留或推进槽也存在未知在途操作时，停止该池新增，按原账本核实后另建恢复阶段。

其他语言可调用`task_pool.py hold --directory ... --project ... --run ... --stage ...`，恢复原账本加`--cleanup`，等待预算用`--wait-seconds`设置。stdout先返回`{"event":"admitted"}`，之后stdin逐行发送`{"id":"唯一请求ID","action":"acquire","progress":false}`或`{"id":"同一请求ID","action":"release"}`；stdout分别返回含相同id的`acquired/released`事件。隔离后普通I/O领取返回`{"id":"唯一请求ID","event":"rejected","error":"pool_dispatch_blocked"}`，该请求不得执行；helper继续接收已有操作的release及`progress:true`的显式清理/回复领取。一次helper可同时等待多个不同id，释放消息不会被前面的等待阻塞；全部操作释放后关闭stdin并等待exit0，仍有未释放操作时EOF报错并保留未知占用。诊断写stderr，异常非零退出；由项目停止相关新增并恢复原账本，不能当作产品请求已取消。

### 预算、失效与恢复

默认30秒采样、1秒窗口、75秒新鲜度；CPU/内存有一项达到80%则暂停新业务并将任务目标容量减1，最低1。两项均低于60%时每周期增加1，60%至80%保持。首次可靠且未高压的采样可直接利用已预留上限。I/O普通容量按任务容量占比调整，另加固定的回复保留槽，总目标不超过显式I/O上限。目标降低后实际占用可能暂时高于目标，等待原任务自然排空；高位槽未释放时不补位，扩容也等待相关高位任务及I/O排空。

高压或unknown期间，新业务case等待；已在途case继续，cleanup最多1个任务、普通I/O保留1槽、回复保留槽继续推进。helper丢失引发的隔离与资源高压不同：`dispatch-blocked.json`阻断当前pool的新业务任务及所有普通I/O领取，包括已准入case的下一次普通请求；已在途操作继续持有其原占用，清理任务和显式推进槽仍可领取。停止、过期、损坏或project/run/stage/directory不匹配时，所有新任务及I/O领取均拒绝，已持令牌仍可释放。恢复时先核实原未知操作已终结、清理责任已记录，再使用新的monitor阶段；不能直接改写旧健康状态或删标记冒充恢复。

任务槽、I/O槽均由操作系统文件锁持有；每个I/O槽在返回acquired前另写持久claim记录，只有收到操作结束后的release才删除。helper异常退出或协议中断会释放内核锁，但保留未知I/O的claim；下一次领取在同一派发锁内检测到claim存在而内核锁空闲，即写隔离标记并拒绝普通I/O，不依赖客户端及时发现退出。未知的推进槽也不补位。客户端发现helper意外退出会拒绝等待者并写当前pool的阻断标记，未丢失的helper仍能释放已有操作并处理显式清理/回复。`runIO`内操作失败与释放失败同时发生时抛出`AggregateError`，按顺序保留原操作错误及释放错误；仅操作失败时保留原错误。产品请求可能仍在途，项目按原账本处理未知结果及cleanup，不能把本地解锁当作数据已清理。锁文件保持原路径，claim与隔离证据不按年龄删除或替换。

`pool.json`固定上下文与上限，`state.json`记录当前目标、实测来源、暂停原因及该次采样的内核锁占用；`samples.jsonl`追加历史。占用数是采样时刻观察，不是完整逐请求计时，helper异常退出后的未知在途操作须结合claim与隔离标记判断，不能从锁占用归零推断请求已结束。参数由实际领取预算决定，脚本不会自行验证远端服务承载能力或提高登录速率。

### 接入验证与用时分析

先用项目实际fixture和客户端做离线验证，再在已获准范围执行代表流程。至少验证跨worker共享总上限、健康启动、80/60滞回、unknown和stale关闭新增、高位排空、命令回复不死锁、cleanup保底、helper异常退出及并行失败等待全部结束。内置离线测试入口：

```sh
python3 -m unittest discover -s "$SKILL_DIR/scripts/tests" -p test_task_pool.py
PYTHON=python3 node --test "$SKILL_DIR/scripts/tests/test_task_pool_client.mjs"
```

两者只使用临时本地文件、合成采样及子进程。POSIX与Windows锁均使用现有平台实现，测试只证明实际运行平台；需要跨平台交付时在对应平台运行。安装脚本或声明worker数不代表项目已接入，报告列出真实覆盖的层次、尚未接入的客户端/协议及验证证据。

用时分析复用`workflow.py timing`和原始Playwright JSON/blob，不为分析重跑产品。按同范围、同环境比较冷登录、CLI墙钟、fixture准备/After Hooks、独立sweep及采集；worker占用须包含teardown，失败重启按`parallelIndex`归并槽位，缺字段时说明限制。嵌套API步骤按时间区间去重；worker和case耗时合计可以大于墙钟，剩余差额保留为尚未分摊，不推断为调度空闲。把采样历史中的实际占用与容量对照，再决定优化依赖分支、轮询等待、证据持久化还是资源上限。离线契约通过只证明并发约束，实际提速只按同口径真实执行报告。

## Agent决策

默认每30秒测量一次，CPU采样窗口1秒。CPU或内存达到80%时，每周期只要求一个最近启动的active sub排空；active sub最低为1。两项都低于60%、有待执行任务及宿主空槽时，每周期增加一个。60%至80%保持，正在排空时不补位。达到下限仍高压时保留执行者、暂停新case，让当前case与cleanup完成后等待。高压下尚未建立sub时先等待资源恢复。

内存比例使用`1 - available / total`，不把文件缓存一律当作不可用内存；Linux cgroup v2有限额时同时测量所在层级及父层级的CPU配额消耗、内存比例，取较高压力。监测只覆盖脚本实际所在的宿主，远端浏览器/执行容器需在实际执行侧接入；可见性或指标缺失记unknown并暂停新增，不能默认为健康。当前cgroup补充采样针对v2，v1/其他限制须由执行侧另外核验，不把整机空闲当作这些隔离域的余量。

低阈值只用于隔离测试。通常保持80/60及30秒周期，避免启动峰值和频繁增减。上限由`max_subagents`控制，并与宿主实际槽位、项目concurrency上限相匹配；只管理本controller拥有的Agent，不按进程名发现后停止其他任务。

## 入口

`scripts/resource_controller.py`提供资源采样及Agent控制入口；case、sweep和I/O池使用上述`task_pool.py`或项目等价实现，不把这些任务伪装成Agent inventory。资源采样使用可选依赖psutil，其他原有工具仍使用标准库。已有兼容安装直接复用；缺失时按当前项目依赖安装授权，将`scripts/requirements-resource-controller.txt`安装到实际执行该脚本的解释器环境。缺失依赖时输出unknown，不能声称采样成功。

```sh
python3 "$SKILL_DIR/scripts/resource_controller.py" sample
```

单次采样、决策及调度门禁状态发布：

```sh
python3 "$SKILL_DIR/scripts/resource_controller.py" step --project "$PROJECT" --input "$CONTROL_INPUT"
```

输入中的inventory必须来自宿主本轮实际状态，started_at包含时区；只包含本controller拥有的sub。state=draining直到当前case、资源清理、报告回执和teardown完成后退出；排空中的sub不接新任务，仍占宿主实际槽位。

```json
{
  "controller_id": "regression-main",
  "settings": {"high_percent": 80, "low_percent": 60, "interval_seconds": 30, "max_subagents": 8},
  "inventory": {
    "workers": [
      {"id": "host-agent-id", "state": "active", "started_at": "2026-11-03T10:00:00+00:00"}
    ],
    "pending_tasks": 12,
    "available_slots": 4
  }
}
```

主Agent使用宿主已有工具执行返回的spawn/drain动作，之后按真实状态更新inventory。长任务用异步会话及有界等待，每个周期重新step；这条路径由主Agent落实定时调用，不声称脚本已后台执行宿主工具。动作失败不能增加虚构Agent，也不能把未清理的Agent标成已退出。

## 自动定时循环与宿主适配器

宿主已有可脚本调用的Agent API或项目worker池时，接入一个adapter；控制器定时调用它，按真实状态完成扩缩容，无需主Agent逐周期重写判断。

```sh
python3 "$SKILL_DIR/scripts/resource_controller.py" watch --project "$PROJECT" --input "$CONTROL_INPUT"
```

watch输入保留controller_id/settings，将inventory换为adapter的argv数组，例如`"adapter": ["python3", "test/agent_adapter.py"]`。命令cwd为PROJECT，不经过shell，不改写PYTHONPATH/NODE_OPTIONS。`--cycles N`可限制周期数，缺省持续监测；stdout每周期一条JSON，主Agent仍须按[进度汇报](workflow.md#进度汇报与长任务等待)向用户汇总资源、调整原因、实际Agent数及用例进度。

adapter从stdin读取一个JSON请求，stdout只返回一个JSON对象，诊断信息写私有日志。inspect返回本controller完整、真实的workers、pending_tasks和available_slots，结构同上inventory，并回显controller_id。每个sub开始新case/claim前检查控制器admission_paused和自身draining状态；持有旧租约不等于可继续接新case。

```json
{"schema_version": 1, "controller_id": "regression-main", "action": "inspect"}
```

spawn/drain请求带稳定action_id。spawn启动一个sub；drain只对指定worker停止接新任务，完成当前case、cleanup及teardown后退出，不发kill、不强关运行中的Agent。drain确认后inspect须将其显示为draining，实际退出后才移出清单。adapter必须按action_id持久化幂等处理，启动中也要保留记录，避免超时重试启动重复Agent。

```json
{"schema_version": 1, "controller_id": "regression-main", "action": "drain", "action_id": "stable-operation-id", "worker_id": "host-agent-id", "admission_paused": true}
```

```json
{"controller_id": "regression-main", "ok": true, "action_id": "stable-operation-id"}
```

控制器在调用动作前原子保存pending_action，动作未确认期间不放行新增；收到ack后再次inspect核验spawn确实登记一个active sub，或指定sub已排空/退出。超时或退出时保留pending，续跑同controller和原adapter先幂等重放，再读真实inventory。适配器失败停止控制循环并关闭新增门禁，由主Agent修复接回；不能删pending或换adapter来跳过未知启动结果。控制器不代替宿主权限、数据授权或残留清理。测试fixture不是生产adapter，不能拿它启动业务回归。

## 与派发门禁衔接

状态位于coordination.root下`test/.cache/scheduling/resource-controllers/<host-hash>/state.json`，同一协调根、同一宿主仅允许一个活动控制器；不同工程共享同一资源池时，使用相同controller_id，adapter的inventory覆盖它拥有的UI和API sub。控制器锁阻止重复循环，保留主Agent的异步句柄以便持续等待和最终结束。

动态模式的每次`scheduling.py acquire/ready`请求顶层传`"resource_controller": "regression-main"`。它检查controller绑定、运行状态及新鲜度，用当前desired_subagents作为跨UI/API共享的执行者容量；各项目显式concurrency及浏览器上限继续有效。容量登记只用于防止同时启动和维护冲突，不再用固定每worker内存或CPU核数推导物理余量。运行中的任务仍保留租约到teardown完成，缩容不会提前释放占用。只读/写资源冲突与残留gate保持原约束。

控制器状态为停止、过期或不可读时拒绝新增，禁止省略resource_controller重试绕回旧估算。旧租约可完成收尾并release/reap；已有租约的幂等acquire不代表可领取下一个case。watch正常结束或可捕获异常时标记stopped；强制终止来不及写终态时，依靠采样新鲜度超时关闭门禁。step的状态也须周期刷新；失效上限为两个检查周期加采样和adapter超时预算。结束监测不自动终止其他Agent，主Agent须先核实本轮sub收尾；采集/清理阶段继续进度汇报。

没有接入动态控制的旧runner保留原估算兼容路径，不能宣称已启用定时扩缩容。迁移时在runner中固定resource_controller参数和上述claim检查，再启用循环；不通过手改调度账本模拟采样。
