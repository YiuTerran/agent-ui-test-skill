# 本地辅助入口

用于减少临时脚本和字段试错。除注明的独立入口外，本页命令经`workflow.py`调用，Python最低3.10、仅用标准库，入口自动检查实际解释器版本；不执行产品测试；`finalize`可调用已安装的本地Playwright离线合并，禁止下载依赖；`valid=true`只描述该命令的检查结果。原有start/renew/record/collect-results等命令继续可用

长回归优先复用项目已有恢复能力；独立入口`checkpoint.py`、Playwright sidecar与`work_packages.py`见[断点调度与有限工作包](checkpoints.md)。sidecar是可选的单次结果适配器，仅用于兼容retry=0、repeatEach=1的suite；保留原有重试/重复执行时使用原生采集，不声称逐case断点复用。配额、冲突组、残留派发门禁及验收照常执行。

指定case复测、只跑自动化及确认来源变更先按[确定性任务](workflow.md#确定性任务直接完成)处理；以下入口用于确需工作流绑定的任务。确定性区间在一次宿主调用内连续完成，不为机械步骤派Agent、逐条研究字段或另写多份摘要。组合入口返回成功后直接交付。

直接回归无需ego-browser或需求来源读取工具；只有新增页面探索、来源核验等对应阶段才检查其依赖。调度请求可用[config_path](contracts.md#双预算与就绪任务)指定项目相对配置路径，或显式null跳过读取无关的test/spec.md；不改变同一PROJECT的共享配额与残留索引。

## 确定性批量入口

### 一次采集

```sh
python3 "$WF" attempt-collect --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle "$BUNDLE"
```

已有真实command.json和JSON/HTML/blob时使用。内部连续执行attempt-bundle→collect-results，复用原门禁，不要求Agent读写中间事件或解析大JSON。返回runner_counts、collection.complete/unresolved和事件路径；映射未完成返回exit2并保留真实已采集状态，不伪造pass，也不自动重跑。相同有效bundle重复采集不重复登记执行。缺产物的真实中断仍走attempt-interrupted，不拿完整采集入口猜结果。

重复执行使用原生JSON采集，保留每个spec/test/result指针和失败历史。导入器支持同一spec下的repeat及声明repeatEach后按spec拆分的重复输出；只有来源位置、suite路径和重复身份均可区分时自动绑定，不由Agent逐条补回执。不同describe中的同名test仍报告歧义。工具问题只修复导入后重用原始产物，不重跑业务或手改JSON。

### 一次收尾

```sh
python3 "$WF" finalize --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$FINALIZE_INPUT"
```

前置：真实CLI均已结束，配额在teardown后释放，资源核验已有真实回执，实际验收决定已明确。程序核对本run无running任务和active配额，不能自动撤销仍在运行的占用。finalize不删除业务资源、不释放未知CLI的配额、不选择或批准业务预期。输入示例：

```json
{
  "acceptance": {
    "status": "completed_with_failures",
    "reason": "按本轮真实测试、覆盖缺口和残留证据填写",
    "evidence": ["test/.cache/runs/<run>/attempts/<bundle>/results/result.json"]
  },
  "require_full": true,
  "events": [],
  "merge": {
    "command": ["corepack", "pnpm@<项目现有版本>", "exec", "playwright"],
    "cwd": ".",
    "timeout_seconds": 120
  }
}
```

- acceptance.status由主Agent依据真实证据明确提供，支持原有fully_accepted/completed_with_failures/partially_blocked/aborted；按[授权复用](workflow.md#授权复用与自动推进)处理，不为填写字段额外等待用户批准。脚本不把exit0推断为验收；fully_accepted仍经过全部原有覆盖/资源/来源/审批门禁
- require_full默认true，要求regression_completeness=complete；用户明确限定定向、仅本地受阻收尾等真实范围时填false，并在reason说明。false不放宽fully_accepted门禁，不表示全量通过
- events可批量带真实resource/issue/merged_report回执；无额外事件用[]或省略。已有登记merge时省略merge；已有真实但未登记merge可在events提供merged_report，不再运行合并。merge与events中的merged_report二选一
- merge.command是项目已安装的Playwright启动argv，脚本追加merge-reports、固定HTML参数和独立输入目录，shell=false。启动形式限于三种：npx/pnpm/yarn/bunx/corepack开头并以playwright结尾（支持Windows包管理器.cmd/.exe）；node/node.exe加node_modules下的@playwright/test/cli.js、playwright/cli.js或playwright-core/cli.js；直接调用playwright可执行文件且不带参数。启动校验与合并回执共用CLI脚本识别规则。拒绝test/install/dlx及-e/--eval/--require/--import等预加载参数。保持项目版本，不自动安装；设置离线环境，将本轮全部已登记blob按bundleID复制并核对摘要，失败批次也包含。每次新合并独立目录，保留旧产物。已登记merge的bundle_ids与当前全部bundle一致时直接交最终report重核；新增CLI后集合不一致，提供merge时重新合并全部blob，未提供则由report核验报错。内容损坏不自动重合并
- 一次调用完成必要merge→批量record真实事件/终态→一次report及严格一致性核验→检查完整性→release。成功只输出报告路径、结果计数、缺口ID、残留及released=true，完整信息已在summary.json，无需Agent再次遍历。没有任何执行的partially_blocked/aborted可在require_full=false时生成如实报告并释放，不补造一致性或测试通过
- 报告/完整性失败返回exit2、stage=report_checks、证据入口及released=false，保留占用和原始结果；资源/issue/merge回执与checkpoint原子落盘，验收终态在报告和完整性检查通过后提交，失败保持可继续采集的原状态；同输入恢复不重复记这些事件。成功结果与释放状态一并保存，成功回执丢失后同owner/同输入直接返回replayed=true，不重写报告。修复对应产物/登记后复用输入，不重跑业务测试。未完成的checkpoint可提交修订后的输入（如改验收终态或require_full），原输入摘要记入superseded_inputs；更换owner时须先显式执行过期租约reclaim并记录原因、完成recovery_review，当前owner才可接续失败/未完成的finalize，接管记录保存在finalization.owner_history。已登记的resource/issue/merged_report继续按事件哈希去重。已成功完成的finalization仅接受原owner、原输入重放，不可转移
- 合并失败保留merge.log和真实command.json，不登记成功、不记录验收终态；进程异常/超时保留现有文件，未取得真实退出码时不造回执。租约/权限错误按原入口处理，不另建run绕过

底层attempt-bundle/collect-results/record/report/release保留用于诊断、真实恢复和特殊兼容；正常路径不与组合入口重复运行。收尾程序完成后直接交付，读完整状态或重建报告只在新的实际异常时进行。

## 统一预检

```sh
python3 "$WF" preflight --project "$PROJECT" --owner "$OWNER"
# 活跃run续租；已取得外部观察和完整发现清单时一起汇总
python3 "$WF" preflight --project "$PROJECT" --run "$RUN" --owner "$OWNER" --input "$PREFLIGHT_INPUT"
```

无`--run`时使用start语义，支持`--scopes/--resume/--new-round`；有`--run`时只续租有效活跃run，配置漂移或真正中断仍需显式恢复。先本地doctor和运行时探测，通过后接管，不回收报告。运行时探测失败返回exit2、`run_started=false`，不新建或续租run，也不释放已有占用。输出run简报、诊断、runtime、sources、coverage_diff及next_steps；有完整发现输入时在state保存其路径、摘要、范围和当前输入指纹，供全量计划绑定，不额外扫描全历史残留

若接管后发现调用者提供的清单/观察无效，返回valid=false及已接管的run简报；保留继承义务与恢复状态，修正输入后使用该run，不另建轮次。已有run的输入检查失败不续租

可选输入：

```json
{
  "runtime": {
    "node": "<项目实际Node可执行文件>"
  },
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
- runtime仅支持可选`node`和`directory`非空字符串，含义与下述独立探测一致；省略node时明确返回`not_checked`，不能视作Node或Playwright就绪。Python文件操作每次preflight自动检查；doctor仍为只读入口
- discovery由项目实际Playwright发现命令提供，例如现有授权CLI的`--list --reporter=json`输出；必须为该run范围完整且未过滤的发现清单，实际setup项目名由项目配置明确提供。`complete=true`是执行者声明，脚本无法独立证明发现命令未过滤，仍需审阅实际命令及配置。按完整文件路径和精确标题匹配，多project和repeat目标保留数量
- 发现结果列出代码/审核缺口、缺失plan、缺失独立test、未映射test及歧义。legacy共享test按(spec,title)归到legacy_shared_tests及关联case_ids，独立化义务保留在code_review_gaps，不重复列作未映射或缺失独立test。未提供发现清单时status=unknown，文件存在不证明独立test存在；已审核有效独立case缺席另列reviewed_missing_tests，阻止把该清单用于全量计划
- 每次发现status=checked时，以当前missing_tests、legacy成员及静态代码资格重新计算automation_gap_case_ids，并同步刷新待选择清单和next_steps。不同的旧发现摘要及结论保存在discovery_inventory_history，相同输入复用；未提供新发现时保留上次依据
- run简报中的automation_expansion按模块列出尚未自动化的case、总数及待选择/获选/未选ID；next_steps中的ask_user_to_choose_automation_expansion_scope表示尚未登记，不证明用户未授权。先按[授权复用](workflow.md#授权复用与自动推进)核对原始指令，已有明确范围则直接登记，缺少决定时才提供按模块新增、全部新增、本轮跳过新增三个选择。已有合格回归可继续；发现不含test的需求须由主Agent补充候选清单
- 来源读取适配器等可复用项目代码放在稳定的测试辅助目录，缓存只保存观察/快照/manifest。缓存缺失按现存manifest重新定位或读取，避免复制依赖旧run路径的脚本

### 运行时探测

独立入口不要求已有工作流资产，直接回归也可调用：

```sh
python3 "$SKILL_DIR/scripts/runtime_probe.py" --project "$PROJECT" --node "$PROJECT_NODE"
```

`PROJECT_NODE`是已核实的项目Node可执行文件；子进程cwd为PROJECT，不替换包管理器或下载依赖。`--node`可省略，结果仅证明Python侧检查。`--directory`默认为`test/.cache/runtime-probes`，只能是`test/.cache/`内的安全项目相对路径，拒绝穿越、符号链接及reparse祖先。完成Attempt准备后，可把该值设为实际`results/output`路径，在同一解释器、Node和继承环境中复检；runner使用不同cwd或额外目录时仍须在其实际运行上下文验证。

脚本创建缺失父目录，只在新建的独立临时子目录中探测并删除该子目录，不清空已有产物。Python检查同目录重复mkdir、进程锁、独占写、fsync、原子替换及回读；Node检查重复mkdir、`.playwright-artifacts-*`临时目录、独占写、fsync与替换回读。返回`python_runtime.version/executable/minimum_version`及注入变量是否存在，不输出环境变量值，也不改写子进程环境。解释器版本低于3.10时在启动时直接报错，不产生有效探测JSON。

`valid=false`或清理失败返回exit2，携带阶段、错误类型，清理失败还保留本次临时目录位置。`fs_mkdir_not_idempotent`使用中性错误码，不推断特定宿主；权限拒绝继续失败。按[运行前提](onboarding.md#运行前提与兼容性)修复后复检，再继续派发。探测通过仅证明当时、该目录及所选运行时的文件操作，不证明浏览器启动、业务访问、全程路径权限或业务清理。

### 范围登记

明确原始请求或范围答复已足够时，通过record登记；下例仅演示按模块新增，值取实际run模块、当前缺口和用户原话：

```json
{
  "type": "automation_expansion",
  "owner": "<OWNER>",
  "mode": "modules",
  "scopes": ["metadata"],
  "gap_case_ids": ["MD-PENDING-1", "OTHER-PENDING-1"],
  "user_reply": "<用户明确原始指令或范围答复的原话>",
  "evidence": ["test/.cache/runs/<run>/automation-scope-decision.md"]
}
```

gap_case_ids包含当前完整缺口清单，既含所选模块，也含未选模块；候选尚未进入coverage时可先使用清单中的case ID。本run全部范围均获准才用mode=all、scopes取全部范围；仅指定模块内全部缺口用mode=modules，不扩大到其他模块。本轮跳过新增用mode=skip、scopes=[]。scope与清单ID限定程序当前可放行的范围，证据保存模块清单、数量、用户原话及原始对话/文件引用；脚本无法独立验证授权语义。

按[授权复用](workflow.md#授权复用与自动推进)，明确全部覆盖目标内、同一来源版本/模块/数据权限内的新缺口可更新完整清单及登记，说明新增ID归属并引用原始授权，无需重复提问；固定清单外或越界时才征询。同一任务的技术新run核实授权未被撤回/收窄、来源预期和数据权限未变后，以当前清单重新登记并引用原证据；不自动继承旧事件、旧任务“仅本轮”的选择或无法核实的历史决定。

## 无副作用事件预校验

```sh
python3 "$WF" validate-input --project "$PROJECT" --run "$RUN" --owner "$OWNER" --type record --input "$EVENTS"
```

type取source、execution、record或report_bundle。前三种在state/coverage内存副本上重放record门禁；report_bundle在内存中走collect-results，包含原始产物与逐次映射。复用当前owner、来源、审批、证据、时序、资源及writer边界，不创建锁文件、不续租、不提交状态或coverage

合法但映射不完整的bundle返回valid=true及collection.complete=false/unresolved，与collect-results语义一致；不能解释为通过。真正提交仍重新检查当前状态。正常固定模板直接提交，只有新字段、混合事件或诊断需要时预校验，避免重复读取大报告

## Playwright进度输出

优先复用项目runner已有精简进度。缺少稳定计数时，可追加随附`scripts/progress_playwright_reporter.mjs`，独立于workflow/checkpoint，不需要manifest，不改变retry/repeatEach，也不写状态、创建产物目录或启动测试。每次CLI有独立计数，不能把多个进程的摘要直接相加当整轮去重结果。

沿用项目Playwright配置中的原有reporters，在数组末尾追加以下条目，路径替换为实际技能目录；`setupProjects`必须来自已核实的本轮配置，无Setup时显式填`[]`：

```js
['<SKILL>/scripts/progress_playwright_reporter.mjs', {
  intervalMs: 30000,
  setupProjects: ['auth'],
}]
```

使用attempt-init覆盖reporter参数时，把该脚本绝对路径加入`extra_reporters`。CLI方式无法传options，可由项目runner设置`E2E_PROGRESS_INTERVAL_MS=30000`和`E2E_PROGRESS_SETUP_PROJECTS='["auth"]'`（JSON数组）；配置options优先。间隔允许1000–60000毫秒，默认30000。不为接入剥离其他环境变量。未提供Setup分类时，`counts`仅表示全部test，`business/setup`为null，不冒充业务数。保留原生JSON/HTML/blob及其实际输出位置，不用进度输出替代正式证据。

脚本在发现完成、每个心跳和runner的onEnd输出一行`E2E_PROGRESS {JSON}`。schema_version=1，sequence为本进程递增序号，phase为testing或runner_ended；另含runner_status、elapsed_seconds、seconds_since_test_event、workers、shard和global_errors数量。只输出计数，不含用例标题、路径、错误正文或凭据。Agent读取新增行后按[汇报要求](workflow.md#进度汇报与长任务等待)转成用户可见简报；宿主若缓存stdout，脚本不能单独保证实时对话。

`counts`及已明确分类后的`business/setup`口径相同：

- total为本次实际发现的test实例数，区分project与repeat；重试不增加total。
- ended=passed+failed+skipped+expected_failed；expected_failed是符合预期的失败，单列不混入真实通过数。failed包括最终失败、超时及预期失败却意外通过。skipped仅统计runner实际提供的跳过结果。
- remaining=total−ended，包含pending、running、retry_pending和interrupted。未知阻塞原因不能从这些计数推断；interrupted保留为未完成。重试耗尽前的失败仍在retry_pending；runner提前结束时按已发生的最后结果结算，无结果的test继续保持pending。
- retried为发生过重试的test数，flaky为重试后符合预期的test数，均不增加ended。串行组重试可能重新运行此前已通过的test，运行中计数会重新打开该实例，实时ended可回退；原生最终报告仍为准。

runner_ended仅表示收到runner终态；不证明资源已清理、配额已释放、报告已采集或整轮已验收。强制终止可能没有终态行，最后心跳仍是旧观察；主Agent说明日志新鲜度并沿正常中断与清理流程处理。配置/发现阶段失败而未收到onBegin时，counts为null；无新test事件的心跳不推断卡死或ETA。

## Attempt准备与事件生成

准备输入是实际执行计划，不含凭据。元数据仍在规范的test目录，已有spec、fixture和Playwright配置沿用原位置，cwd取实际项目命令目录，不为接入搬迁代码：

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

可选`extra_reporters`为现有reporter文件路径数组：项目内相对路径从PROJECT解析；项目外绝对路径仅允许当前技能随附的`checkpoint_playwright_reporter.mjs`和`progress_playwright_reporter.mjs`。准备时核对文件存在、路径无逗号且不重复，计划保存解析后的绝对路径，返回的reporter参数在list/json/html/blob后追加它们。CLI的`--reporter`会替换配置中的reporters，不能只在Playwright配置里追加sidecar后继续使用默认覆盖参数。其他自定义reporter或需要options的reporter也可保留在项目配置中，同时配置原生JSON/HTML/blob及对应输出位置，并省略返回的reporter参数组；最终仍按真实产物核验。

先在同级临时目录准备plan.json和全部子目录，再整体rename发布到`attempts/cli-01`；写入或发布失败只清理本次临时目录，同一个bundle ID可重试。进程被直接终止而遗留的点开头临时目录由retention保留并跳过，不作为bundle回收。返回JSON/HTML/blob环境变量及reporter/output参数数组，其中输出目录是当前执行主机的绝对路径。CI容器、挂载位置或cwd不同，应在实际执行文件系统中准备Attempt并应用对应路径。由执行者使用当前shell的安全引用方式应用，不把JSON数组直接拼成shell命令。沿用项目安装版本和密钥注入，领取真实配额后执行，teardown完成才释放

实际CLI回执保存到该attempt的command.json，包含完整真实command数组、cwd、environment、scope、setup_projects、started_at、ended_at和整数exit_code；仅回执中断的未知退出码见下文。command必须等于计划command，或仅追加返回的reporter/output参数组；两组可分别省略或换序，每组至多一次，输出路径须完全匹配。`--grep`、`--project`、spec路径、`--shard`等执行选择参数必须在计划command中明确声明。起止时间带时区、在准备之后且不在未来，退出码由真实进程观察提供，失败也保留回执和全部产物

regression_mode与收尾完整性：

- affected为缺省的受影响验证；收尾全量明确填full，绑定当前preflight的完整发现清单、范围和输入指纹，要求非空业务目标且不存在已审核独立test缺席。保留项目原有retry/repeatEach语义；重复执行时以相同选择和重复参数生成发现清单，避免实际目标倍数与清单不符，不为full或sidecar关闭重试/重复执行
- full计划保存全部发现spec（含Setup）、发现JSON的config.configFile及项目根playwright.config.*的SHA-256。config.rootDir小于项目根时默认纳入整棵测试目录树；等于项目根时仅纳入spec、配置及spec父链的fixtures目录。目录遍历排除node_modules、.cache、__pycache__，以及运行期产物目录.auth、test-results、playwright-report、blob-report和发现JSON中各project的outputDir；storageState等写在其他位置的生成文件用可选code_excluded_paths（项目相对路径数组，不能为项目根）声明。文件增删也参与比对。旧计划缺排除字段时按原排除规则比对。可选code_roots字符串数组扩展项目内文件或目录；路径均校验符号链接和越界
- preflight的coverage_diff及full的attempt-init输出code_roots_suggested、code_references_outside_snapshot和code_import_warnings。递归扫描spec与配置的字面量相对import/export/require，提示范围外依赖、未解析引用和项目外引用；提示不自动扩展指纹或阻断执行。审阅后将需要的code_roots传入full计划。此扫描不是模块解析器，不覆盖别名或动态计算路径
- 验收与报告重算当前内容，变化标为code_changed_after_full并判incomplete；旧版窄范围指纹或缺指纹标为code_fingerprint_missing，需要新full证明当前代码，原始执行回执仍保留
- 采集预期与实际的(file,title,project)多重集合，比较缺失/多余目标、Setup和shard。skipped/notrun保留在目标集合中；集合齐全与业务通过分别记录，差集写入summary、Markdown及HTML
- native_merge_required=true的run每个bundle必须绑定当前schema的Attempt计划。fully_accepted要求regression_completeness=complete，即使所有直接execution都通过也适用；未登记bundle、仅affected或缺目标/分片时均为incomplete，保留证据并阻止完整验收
- not_declared仅用于native_merge_required缺省或false的旧run未声明用途的兼容记录。完整性相对于执行者提供的发现清单，仍须审阅真实发现命令及配置

单次CLI只收一个有效Playwright blob。每个ZIP必须有可识别的onBlobReportMetadata，额外ZIP、未知格式或shard不符均拒绝；分片各用独立Attempt，command明确声明`--shard=N/T`，JSON和blob中的shard必须一致。全量收尾按同一发现清单、输入及代码指纹汇总全部N=1…T分片的业务集合，每片保留自己的Setup；同一分片重跑使用新bundle ID，汇总采用该分片最新尝试并保留历史记录

以下是诊断/恢复用的底层接口，正常用attempt-collect，无需先调用它：

```sh
python3 "$WF" attempt-bundle --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle cli-01
```

默认读取command.json，也可用`--input <项目相对回执路径>`，须位于本attempt。核对计划身份、输入指纹、完整命令契约和真实JSON/HTML/blob后，排他生成bundle-event.json，绑定plan路径和摘要并返回collect-results所需路径。新run的计划绑定门禁统一用于record、直接collect、结果导入及后续report重核。存在且内容相同时可复用，内容变化拒绝覆盖；failed/retry全部保留

attempt-init拒绝覆盖已有attempt、越界scope、异常路径和符号链接。中断后保留plan与证据，诊断现有attempt，实际重跑换bundle ID。正常直接attempt-collect；只有定位产物问题时用attempt-check，它不生成事件。

技术恢复会轮换freshness_marker。进程已结束、原生文件和真实回执完整，但旧Attempt尚未采集时，主Agent核对环境、原件及当前输入后，在本次recovery_review显式绑定原plan及其文件字节SHA-256，无需用户额外批准该登记：

```json
{
  "type": "recovery_review",
  "environment_checked": true,
  "artifacts_checked": true,
  "attempt_plans": [{
    "attempt_plan": "test/.cache/runs/<run>/attempts/cli-01/plan.json",
    "attempt_plan_sha256": "<原plan.json字节的SHA-256>"
  }]
}
```

绑定必须属于同一run、当前scope内的恢复前计划，配置及coverage指纹一致；普通恢复勾选不替代绑定，跨run、摘要不符或输入已变均拒绝。完成当前来源核验后，attempt-collect和直接collect-results使用同一绑定门禁；原plan、marker和产物不改写，bundle记录recovery_review归属。已采集结果可继续复用；恢复采集的full也经过同一发现目标、全部shard及当前代码指纹检查，满足时可参与完整性判断，summary以recovered_bundle_ids列出。真实环境或预期变化、代码漂移和失效case仍按原门禁处理，不能把技术恢复登记当作新的执行。

进程已退出且JSON/HTML/blob不齐的受控中断，用同一plan与真实command.json生成仅回执事件，再按返回的next_command调用record：

```sh
python3 "$WF" attempt-interrupted --project "$PROJECT" --run "$RUN" --owner "$OWNER" --bundle cli-01 --reason "<实际中断原因>"
```

要求非零整数退出码，或显式exit_code:null；后者用于已确认进程结束但宿主会话结束、外部终止等导致拿不到退出状态的情况，command.json须带reason解释原因，且与--reason一致。起止时间仍需真实有效、计划/命令及文件摘要一致；--input可指定本attempt内的其他真实回执。完整产物继续走attempt-collect。interrupted_attempt单列于state、HTML、Markdown和timing，仅计CLI墙钟，测试时长保持null，不进入业务结果、full完整性或原生merge输入。未知退出码在机器数据保留null、可读报告显示unknown。计划和命令回执持续保留，资源恢复及配额释放仍依据实际状态；未观察到进程退出时先保留issue及现有证据，不造结束时间或退出码

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
