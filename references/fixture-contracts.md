# fixture离线行为契约

fixture新增或修改部分初始化、资源清理、共享配置恢复、缺席判定或敏感响应处理时，用项目真实实现做故障注入验证，再派发依赖它的业务写测试。优先复用项目已有等价契约测试；缺少时接入`scripts/fixture_contract_probe.py`。UI/API都可使用，不要求每个项目支持全部资源类型；跨进程互斥仍用[锁契约检查](local-tools.md#fixture锁契约检查)，不重复造锁。

探针提供受控loopback HTTP服务，分别核对真实请求、服务端最终状态、从fixture持久化账本重读的规范化导出和临时产物。adapter的`valid:true`、仅构造成功receipt或另写一套演示fixture均不能作为项目接入证明。业务预期与接口语义仍按需求和项目契约审核，不能从本探针的合成服务反推。

## 输入与执行门禁

```json
{
  "command": ["node", "test/scripts/fixture-contract-adapter.mjs"],
  "inputs": [
    "test/scripts/fixture-contract-adapter.mjs",
    "test/src/fixtures/resources.ts",
    "test/src/fixtures/recovery.ts"
  ],
  "contracts": ["partial", "config", "absence"],
  "timeout_seconds": 10
}
```

`command`为已核实的本地adapter argv，shell=false、cwd=PROJECT、继承环境，不安装依赖。`inputs`非空，列出adapter、实际fixture及影响行为的依赖和非敏感配置；全部是项目相对文件，拒绝穿越、符号链接和reparse祖先。检查前后核对字节未变，结果`adapter_fingerprint`绑定声明文件hash、命令、项目、协议、探针及参数；不自动发现缺失依赖，也不是可缓存的放行凭证。

`contracts`默认上述三组，可按真实fixture职责显式选择非空子集。`partial`检查资源准备与失败清理；`config`检查共享配置恢复；`absence`检查缺席语义。输出列明实际检查的组；未选不代表通过，不能为规避失败删除适用组。`timeout_seconds`是每次adapter执行上限，范围1–30秒。`directory`可选，默认`test/.cache/fixture-contract-probes`，仅允许`test/.cache/`下无symlink/reparse的路径。探针创建父目录及独立临时子目录，仅删除本次子目录，保留同级既有产物。

项目runner可调用脚本并检查退出码，也可使用已实现的guarded spawn：

```sh
python3 "$SKILL_DIR/scripts/fixture_contract_probe.py" \
  --project "$PROJECT" --input "$FIXTURE_PROBE_INPUT" \
  --run python3 "$RUNNER" "$@"
```

`--run`之后全部是项目既有runner的argv，省略时只检查。脚本每次重新执行所选探测，输出一条不含敏感原文的JSON；失败exit2且不启动runner，成功才以同cwd、同继承环境启动并透传runner退出码和输出。`--run`须位于参数最后。runner的正常日志、权限、quota、checkpoint/residual gate、业务执行及teardown仍沿原路径；探针不代领配额、不修改真实残留，也不把runner exit0当作业务验收。

将门禁放在实际执行路径的产品CLI spawn之前，不能只生成配置或安装脚本。首次接入验证adapter缺失、行为检查失败均不会启动产品CLI；fixture、adapter、声明输入、运行时或环境变化后重新验证。若使用已有等价契约测试，也须有实际失败阻断路径，不能捕获检查失败后继续。脚本不复用历史成功JSON，项目若另行缓存须自行实现完整失效条件。

## adapter协议

每阶段先启动execute进程，stdin首行UTF-8 JSON：

```json
{
  "protocol_version": 1,
  "operation": "execute",
  "scenario": "partial",
  "directory": "<本阶段绝对临时目录>",
  "base_url": "http://127.0.0.1:<临时端口>",
  "observation_path": "<同目录下observation.json>"
}
```

adapter直接导入实际项目fixture，将其HTTP传输/路由/响应适配到合成服务，将账本、附件和恢复路径全部重定向到`directory`。使用合成身份，不访问业务API、真实账号或项目既有账本，不派生后台子进程。若项目fixture无法重定向这些依赖，先修测试层可注入性，或保留已有等价离线测试；不能复制另一套业务清理实现来凑协议。

stdout/stderr允许诊断但会受限捕获和泄密检查，不回显。execute直接调用实际fixture，捕获本场景预期的准备/清理失败，按原机制持久化账本，exit0表示场景执行完毕而非全部资源已清。意外错误非零退出，探针阻断。配置三个阶段共用同一个目录，其余场景各用独立目录。

execute退出后，探针删除先前`observation_path`，**另起observe进程**，传相同阶段、目录和路径，但`operation=observe`且不提供`base_url`。observe使用项目实际账本/恢复overlay读取入口，从磁盘重新加载状态并规范化到`observation_path`，不能调用HTTP或重新执行清理；探针会拒绝期间新增的HTTP请求。不能导出execute内存、把预先生成的observation当真实账本，或在adapter中推算应该有的状态。正常与失败清理均由实际fixture落盘，文件扫描本身不证明记账完成。每次execute/observe各自受timeout限制，产物在execute后及observe后均扫描。

### partial：部分初始化与独立清理

- `POST /resources`前两次返回201，JSON含`id=first/second`，第三次返回400；准备到第三次失败后，fixture仍应清理已知成功资源。
- `DELETE /resources/first`固定503，资源仍存在；`DELETE /resources/second`返回204并删除。即使第一个失败，也须继续安全独立分支，随后`GET /resources/second`取得404回读。
- `GET /resources/<id>`存在时200，不存在时404。导出形如：

```json
{
  "resources": [
    {"id": "first", "status": "cleanup_failed"},
    {"id": "second", "status": "absence_verified"}
  ]
}
```

`first`必须保持未决，允许`intent/confirmed/ambiguous/cleanup_failed`；`second`必须真实删除并回读后为`absence_verified`。以请求日志和最终服务状态交叉核验，只有成功导出不能通过。

### config：原值、失败恢复与已结案重入

1. `restore`：先`GET /config`得到`{"value":"baseline"}`并保存原snapshot，再`PUT /config`设置`{"value":"temporary"}`并GET回读。恢复原值的PUT固定503，仍是temporary，账本必须未决。
2. `recover`：新进程读取同目录真实待恢复账本，服务此时允许恢复；须PUT baseline并GET回读，保存恢复snapshot及结案证据，原snapshot字节不变。
3. `recovered`：探针将服务配置模拟为其他任务后来合法设置的`later-legitimate-change`，再次运行真实恢复入口。不得重写旧baseline，原snapshot及已结案恢复snapshot均保持原字节，当前持久化账本仍须指向相同且存在的snapshot，并保持结案。

导出第一阶段只需`status`为上述未决之一及`original_snapshot`；恢复成功后导出：

```json
{
  "status": "absence_verified",
  "original_snapshot": "original.json",
  "restored_snapshot": "restored.json"
}
```

snapshot路径相对于本阶段目录，内容为规范化`{"value":"..."}`。项目原件格式不同时，可另外导出规范化snapshot，但必须从实际固定的原件/恢复回读转换，不能重取当前配置冒充原值。这里的`absence_verified`仅映射“恢复义务已按原值关闭”，不表示删除配置；沿用现有shared_config残留状态语义，不变更真实receipt格式。探针不证明尚未观察的写前intent时序，项目仍须验证真实记账顺序。

### absence：可见性、权限和未完整枚举

| scenario | 服务行为 | 必须观察与导出 |
| --- | --- | --- |
| hidden | `GET /resources`返回空items，但`GET /resources/hidden`返回200 | 实际查询列表及准确ID；`id=hidden`保留未决，不能仅由默认列表为空判缺席 |
| denied | `GET /resources/hidden`返回403 | 实际准确ID查询；`id=hidden`保留未决，权限拒绝不能判缺席 |
| paginated | `GET /resources`返回`items=[]/next="2"/total=1`；带query的后续页503 | 实际列表查询；原义务`id=unknown-create`保持未决，不能由第一页为空或分页失败结案 |

导出统一为`{"resources":[{"id":"hidden","status":"ambiguous"}]}`等。`unknown-create`是合成未知创建义务的关联标识，不是物理资源ID。以上场景只有GET，禁止为满足探针删掉服务中仍存在的对象。项目真实缺席查询、权限隐藏、软删除及分页语义另按[资源恢复契约](cleanup.md#项目资源恢复契约)验证。

## 敏感信息与证据边界

合成资源成功及错误响应含每轮随机secret，探针扫描adapter stdout/stderr和本次目录中的账本、附件、恢复产物及文件名。普通失败、非零退出和超时后仍扫描；发现泄露返回`secret_leak`，不输出secret、命令、文件名或原始错误消息。当前检查原字节、UTF-16、hex、常见base64及有限深度zip/gzip；限制单文件/单输出2MiB、扫描预算16MiB、文件目录总数256、archive entries128、编码/归档深度3，超限也阻断，不解压到文件系统。

结果`http_observations`保留各阶段合成请求数量及最多64条method/白名单endpoint/status，不输出query、body、真实地址或任意路径。原始临时账本、快照及失败附件统一删除，避免保留泄密反例；JSON中的stage、checks及合成轨迹用于定位，不声称保存了原始业务证据。

这只能发现合成canary的受测泄漏路径，不能证明所有真实凭据、任意编码和全部产物均已脱敏，也不能验证未声明依赖、未导出状态或adapter是否诚实复用fixture；这些仍须定点代码审查。HTTP服务及临时目录不是网络或文件系统沙箱，必须先核实adapter纯离线且路径已注入；宿主限制loopback时使用宿主支持的执行权限路径，不剥离环境注入或绕过策略。

成功结果只证明本轮所选离线行为，不能生成业务通过、资源absence或清除生产残留。实际写入、部分初始化失败和中断后的归属账本、独立sweep、恢复overlay及跨run索引仍按[清理契约](cleanup.md)执行。
