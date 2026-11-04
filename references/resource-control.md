# 定时资源控制

控制Agent并发，最低保留1主Agent和1个sub；主Agent负责协调，调度账本中的agents表示实际领取工作包的执行者，不包含仅协调的主Agent。Playwright内部worker数保持项目已确认的较小值，不随Agent数一起增长。没有独立任务、宿主槽位或可靠采样时不扩容；宿主没有子Agent能力时由主Agent继续。

## 决策

默认每30秒测量一次，CPU采样窗口1秒。CPU或内存达到80%时，每周期只要求一个最近启动的active sub排空；active sub最低为1。两项都低于60%、有待执行任务及宿主空槽时，每周期增加一个。60%至80%保持，正在排空时不补位。达到下限仍高压时保留执行者、暂停新case，让当前case与cleanup完成后等待。高压下尚未建立sub时先等待资源恢复。

内存比例使用`1 - available / total`，不把文件缓存一律当作不可用内存；Linux cgroup v2有限额时同时测量所在层级及父层级的CPU配额消耗、内存比例，取较高压力。监测只覆盖脚本实际所在的宿主，远端浏览器/执行容器需在实际执行侧接入；可见性或指标缺失记unknown并暂停新增，不能默认为健康。当前cgroup补充采样针对v2，v1/其他限制须由执行侧另外核验，不把整机空闲当作这些隔离域的余量。

低阈值只用于隔离测试。通常保持80/60及30秒周期，避免启动峰值和频繁增减。上限由`max_subagents`控制，并与宿主实际槽位、项目concurrency上限相匹配；只管理本controller拥有的Agent，不按进程名发现后停止其他任务。

## 入口

`scripts/resource_controller.py`是独立入口。它的资源采样使用可选依赖psutil，其他原有工具仍使用标准库。已有兼容安装直接复用；缺失时按当前项目依赖安装授权，将`scripts/requirements-resource-controller.txt`安装到实际执行该脚本的解释器环境。缺失依赖时输出unknown，不能声称采样成功。

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
