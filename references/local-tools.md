# 本地辅助入口

用于减少临时脚本和字段试错。所有命令经`workflow.py`调用，Python3.10+标准库，无网络访问，不执行产品CLI；`valid=true`只描述该命令的检查结果。原有start/renew/record/collect-results等命令继续可用

## 统一预检

```sh
python3 "$WF" preflight --project "$PROJECT" --owner "$OWNER"
# 活跃run续租；已取得外部观察和完整发现清单时一起汇总
python3 "$WF" preflight --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$PREFLIGHT_INPUT"
```

无`--run`时使用start语义，支持`--scopes/--resume/--new-round`；有`--run`时只续租有效活跃run，配置漂移或真正中断仍需显式恢复。先本地doctor，通过后接管，不回收报告。输出run简报、诊断、sources、coverage_diff及next_steps；有完整发现输入时在state保存其路径、摘要、范围和当前输入指纹，供全量计划绑定，不额外扫描全历史残留

若接管后发现调用者提供的清单/观察无效，返回valid=false及已接管的run简报；保留继承义务与恢复状态，修正输入后使用该run，不另建轮次。已有run的输入检查失败不续租

可选输入：

```json
{
  "sources": [
    {
      "manifest": "test/.cache/sources/requirements/manifest.json",
      "observation": {
        "source_id": "requirements",
        "readable": true,
        "observed_at": "<本轮实际读取时间>",
        "revision": "<真实版本或null>",
        "fingerprint": "<可靠内容摘要>",
        "fingerprint_basis": "canonical_content",
        "evidence": "test/.cache/runs/<run>/sources/requirements/observation.json"
      }
    }
  ],
  "discovery": {
    "path": "test/.cache/runs/<run>/discovery/all-tests.json",
    "complete": true,
    "setup_projects": ["auth"]
  }
}
```

- 来源观察由连接器或项目既有读取工具提供，preflight只复用source_cache.verify的身份和完整性检查。观察时间不得早于本轮创建或最新resume；新建run的首次预检通常尚无观察，输出observe，随后按现有source流程读取、核验和登记即可，无需再跑一次全量preflight。未提供来源观察时，不推断远端未变
- discovery由项目实际Playwright发现命令提供，例如现有授权CLI的`--list --reporter=json`输出；必须为该run范围完整且未过滤的发现清单，实际setup项目名由项目配置明确提供。`complete=true`是执行者声明，脚本无法独立证明发现命令未过滤，仍需审阅实际命令及配置。按完整文件路径和精确标题匹配，多project和repeat目标保留数量
- 发现结果列出代码/审核缺口、缺失plan、缺失独立test、未映射test及歧义。legacy共享test按(spec,title)归到legacy_shared_tests及关联case_ids，独立化义务保留在code_review_gaps，不重复列作未映射或缺失独立test。未提供发现清单时status=unknown，文件存在不证明独立test存在；已审核有效独立case缺席另列reviewed_missing_tests，阻止把该清单用于全量计划
- 每次发现status=checked时，以当前missing_tests、legacy成员及静态代码资格重新计算automation_gap_case_ids，并同步刷新待选择清单和next_steps。不同的旧发现摘要及结论保存在discovery_inventory_history，相同输入复用；未提供新发现时保留上次依据
- run简报中的automation_expansion按模块列出尚未自动化的case、总数及待选择/获选/未选ID；有未选择缺口时next_steps提示ask_user_to_choose_automation_expansion_scope。盘点完成后向用户提供按模块新增、全部新增、本轮跳过新增三个选择，已有合格回归可继续。发现不含test的需求须由主Agent补充候选清单
- 来源读取适配器等可复用项目代码放在稳定的测试辅助目录，缓存只保存观察/快照/manifest。缓存缺失按现存manifest重新定位或读取，避免复制依赖旧run路径的脚本

用户作出选择后，通过record登记一次；下例仅演示按模块新增，值取实际run模块、缺口和答复：

```json
{
  "type": "automation_expansion",
  "owner": "<OWNER>",
  "mode": "modules",
  "scopes": ["metadata"],
  "gap_case_ids": ["MD-PENDING-1", "OTHER-PENDING-1"],
  "user_reply": "<用户明确选择的原话>",
  "evidence": ["test/.cache/runs/<run>/automation-scope-decision.md"]
}
```

gap_case_ids包含向用户展示的完整缺口清单，既含所选模块，也含未选模块；候选尚未进入coverage时可先使用清单中的case ID。全部新增用mode=all、scopes取本run全部范围；本轮跳过新增用mode=skip、scopes=[]。所选scope与清单ID共同限定授权；清单外缺口再次征询。证据中保存模块清单、数量和真实答复，user_reply由执行者忠实登记，脚本无法独立验证其来源。新轮不继承选择；同轮仍适用的明确授权直接复用

## 无副作用事件预校验

```sh
python3 "$WF" validate-input --project "$PROJECT" --run "$RUN" --owner "$OWNER" --type record --input "$EVENTS"
```

type取source、execution、record或report_bundle。前三种在state/coverage内存副本上重放record门禁；report_bundle在内存中走collect-results，包含原始产物与逐次映射。复用当前owner、来源、审批、证据、时序、资源及writer边界，不创建锁文件、不续租、不提交状态或coverage

合法但映射不完整的bundle返回valid=true及collection.complete=false/unresolved，与collect-results语义一致；不能解释为通过。真正提交仍重新检查当前状态。正常固定模板直接提交，只有新字段、混合事件或诊断需要时预校验，避免重复读取大报告

## Attempt准备与事件生成

准备输入是实际执行计划，不含凭据：

```json
{
  "command": ["pnpm", "exec", "playwright", "test"],
  "cwd": ".",
  "environment": "<实际授权环境>",
  "scope": ["metadata"],
  "setup_projects": ["auth"],
  "regression_mode": "affected"
}
```

```sh
python3 "$WF" attempt-init --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle cli-01 --input "$ATTEMPT_INPUT"
```

先在同级临时目录准备plan.json和全部子目录，再整体rename发布到`attempts/cli-01`；写入或发布失败只清理本次临时目录，同一个bundle ID可重试。进程被直接终止而遗留的点开头临时目录由retention保留并跳过，不作为bundle回收。返回JSON/HTML/blob环境变量及reporter/output参数数组，其中输出目录是当前执行主机的绝对路径。CI容器、挂载位置或cwd不同，应在实际执行文件系统中准备Attempt并应用对应路径。由执行者使用当前shell的安全引用方式应用，不把JSON数组直接拼成shell命令。沿用项目安装版本和密钥注入，领取真实配额后执行，teardown完成才释放

实际CLI回执保存到该attempt的command.json，包含完整真实command数组、cwd、environment、scope、setup_projects、started_at、ended_at和整数exit_code。command必须等于计划command，或仅追加返回的reporter/output参数组；两组可分别省略或换序，每组至多一次，输出路径须完全匹配。`--grep`、`--project`、spec路径、`--shard`等执行选择参数必须在计划command中明确声明。起止时间带时区、在准备之后且不在未来，退出码由真实进程观察提供，失败也保留回执和全部产物

regression_mode与收尾完整性：

- affected为缺省的受影响验证；收尾全量明确填full，绑定当前preflight的完整发现清单、范围和输入指纹，要求非空业务目标且不存在已审核独立test缺席
- full计划保存发现清单内全部spec（含Setup）及其父链中fixtures目录的文件集合与SHA-256；目录增删文件也参与比对。非惯例fixture/helper目录通过可选code_roots字符串数组显式纳入。验收与报告重算当前内容，变化标为code_changed_after_full并判incomplete；旧计划缺指纹标为code_fingerprint_missing，原始执行回执仍保留
- 采集预期与实际的(file,title,project)多重集合，比较缺失/多余目标、Setup和shard。skipped/notrun保留在目标集合中；集合齐全与业务通过分别记录，差集写入summary、Markdown及HTML
- native_merge_required=true的run每个bundle必须绑定当前schema的Attempt计划。fully_accepted要求regression_completeness=complete，即使所有直接execution都通过也适用；未登记bundle、仅affected或缺目标/分片时均为incomplete，保留证据并阻止完整验收
- not_declared仅用于native_merge_required缺省或false的旧run未声明用途的兼容记录。完整性相对于执行者提供的发现清单，仍须审阅真实发现命令及配置

单次CLI只收一个有效Playwright blob。每个ZIP必须有可识别的onBlobReportMetadata，额外ZIP、未知格式或shard不符均拒绝；分片各用独立Attempt，command明确声明`--shard=N/T`，JSON和blob中的shard必须一致。全量收尾按同一发现清单、输入及代码指纹汇总全部N=1…T分片的业务集合，每片保留自己的Setup；同一分片重跑使用新bundle ID，汇总采用该分片最新尝试并保留历史记录

```sh
python3 "$WF" attempt-bundle --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle cli-01
```

默认读取command.json，也可用`--input <项目相对回执路径>`，须位于本attempt。核对计划身份、输入指纹、完整命令契约和真实JSON/HTML/blob后，排他生成bundle-event.json，绑定plan路径和摘要并返回collect-results所需路径。新run的计划绑定门禁统一用于record、直接collect、结果导入及后续report重核。存在且内容相同时可复用，内容变化拒绝覆盖；failed/retry全部保留

attempt-init拒绝覆盖已有attempt、越界scope、异常路径和符号链接。中断后保留plan与证据，诊断现有attempt，实际重跑换bundle ID；恢复造成来源marker/输入变化时，先明确审阅其证据归属。正常直接attempt-bundle；只有定位产物问题时用attempt-check，它不生成事件

进程已退出且JSON/HTML/blob不齐的受控中断，用同一plan与真实command.json生成仅回执事件，再按返回的next_command调用record：

```sh
python3 "$WF" attempt-interrupted --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle cli-01 --reason "<实际中断原因>"
```

要求非零整数退出码、有效起止时间、计划/命令一致及文件摘要；--input可指定本attempt内的其他真实回执。完整产物继续走attempt-bundle/collect-results。interrupted_attempt单列于state、HTML、Markdown和timing，仅计CLI墙钟，测试时长保持null，不进入业务结果、full完整性或原生merge输入。计划和命令回执持续保留，资源恢复及配额释放仍依据实际状态；未观察到进程退出时先保留issue及现有证据，不造结束时间或退出码

## 只读耗时汇总

```sh
python3 "$WF" timing --project "$PROJECT" --run "$RUN"
```

不要求活跃租约，不写历史文件。输出工作流区间、已记录phase_time的分阶段并集、CLI区间总和/并集（含单列的interrupted仅回执调用）、每批result.duration总和/实际区间并集及未归因墙钟。并行result时长可超过CLI墙钟，差额用区间并集计算。已回收JSON及仅回执调用的逐test数据保留null，现存JSON与中断回执重核登记摘要；本命令不替代报告一致性核验

来源时间从本run的`sources/*/observation.json`读取started_at/observed_at，按并集统计并行读取，不重读正文。phase_time可以粗粒度记录source/exploration/code/preparation/runner/finalization；沿用实际已有区间，不要求逐动作计时文件。各阶段可能重叠，不能相加解释为总耗时

终态run优先使用summary.report_generation.completed_at作为观察终点，旧报告回退到summary.generated_at，没有summary时用最后事件时间；活跃run使用当前时间。输出report_generation单列报告生成区间，完成采样不含最后summary落盘、stdout、释放租约及最终答复；summary.timing.wall_seconds仍保留原汇总取样口径。工作流外的会话边界缺省null；复盘时先核查当前宿主日志的实际请求、范围选择答复和最终交付时间，再提供证据支持的边界，无法恢复则保持null。可选`--input`：

```json
{
  "session": {
    "started_at": "<请求的实际时间戳>",
    "ended_at": "<最终答复的实际时间戳>",
    "evidence": ["test/.cache/runs/<run>/timing/host.jsonl"],
    "log": "test/.cache/runs/<run>/timing/host.jsonl",
    "host_format": "claude_code"
  }
}
```

log可省略，只登记调用者提供的边界及证据。提供log时按`host_format`选择解析函数，取值对应当前宿主：

| 宿主 | host_format | 日志来源 | 计数口径 |
| --- | --- | --- | --- |
| Claude Code | `claude_code` | `~/.claude/projects/<项目路径编码>/<session_id>.jsonl`，复制到run的timing目录 | tool_use/tool_result按ID配对；nonzero_tool_results取开头为`Exit code N`的结果，error_tool_results另计is_error（含权限拒绝）；tokens按message.id去重累计input/cache_creation/cache_read/output |
| Codex | `codex` | 会话rollout JSONL | response_item的function/custom_tool调用及回执；nonzero_tool_results取`Process exited with code N`；tokens为null |

缺省`auto`由各解析函数分别识别，恰好一种格式命中才计数；无命中返回host_log_status=unsupported_host_log，多种命中返回ambiguous_host_log，计数均为null。新宿主在`workflow_tools.py`的`HOST_LOG_PARSERS`中增加一个逐行解析函数。识别后验证日志边界，此时没有工具调用可为0。

Claude Code的tokens只覆盖所给transcript，子Agent的transcript为独立文件；任一字段缺失时该字段及total_tokens为null。需要登记整轮用量时，主Agent据此写usage事件并声明coverage。输出不包含参数/原始回执。模型CPU、精确网络等待及未测阶段保持null/unknown，不作固定提速承诺
