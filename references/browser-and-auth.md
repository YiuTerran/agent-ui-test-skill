# 浏览器、认证与人工恢复

## 浏览器能力与隔离

执行者读取当前安装的ego-browser技能及真实集成文档后使用ego-lite，不假设工具名、Space或隔离语义。已有有效会话可复用，先验证当前身份及应用实际具备的角色、组织/租户和授权范围。

可选的浏览器/工具更新提示不属于业务前置；已有能力可用时继续场景探索，不为升级结束会话或新增用户确认。只有版本确实阻断当前操作时才记录具体错误并处理依赖。

Space是操作分配单位，不自动证明cookie或服务端状态隔离；不同tab/context也不能证明隔离。同账号不直接等于冲突：按账号/组织切换、全局配置及共享数据建立冲突组，非冲突scope与独立数据可并行。探索浏览器与所有活跃Playwright workers共享默认上限4，资源不足收紧；开始前领取配额，teardown完成才释放。浏览器句柄是临时信息，恢复依据是持久化路由、就绪条件、身份引用及资源账本。

## 认证模式

| 模式 | 目的 | 就绪证据 |
| --- | --- | --- |
| exploration | ego-lite发现流程与可行性 | 已批准会话，核实预期身份及适用的角色/组织 |
| local_automation | 本地Playwright执行 | 人工准备状态或受支持bootstrap，在fresh headless context验证 |
| ci_automation | 无人值守/CI执行 | 应用支持的自动bootstrap、密钥注入、所需网络和角色在实际CI验证 |

应用无需登录时认证方法记为none，跳过认证准备，写入仍按数据scope和owned规则执行。能力模式、应用方法（如none/api/manual）、显示方式（headless/headed）和执行地点（local/CI）是不同字段，分别记录。没有API登录不妨碍已批准的交互探索或本地试验；仅有探索会话时，CLI验证仍为not run。

本地自动登录成功只证明该本地路径。CI的网络、秘密注入、依赖和角色配置需要CI执行证据，不能由本地fresh headless通过推导。

## 探索会话

复用有效ego-lite会话，不为每case重复登录。CAPTCHA、MFA、二维码登录和组织选择交由人工完成，依[人工等待与恢复](#人工等待与恢复)处理。写入前验证预期身份及数据scope，身份切换会使依赖原身份的就绪证据失效。

ego-lite会话、browser ref与Playwright context不自动互通。通过本地工具及应用实际契约准备状态，不把cookie、token、凭据或完整认证状态传入模型消息。

### 页面定位与探索复用

首次进入业务模块时，用当前ego-browser实际支持的API识别顶层页面及候选iframe，按URL、可见标题和关键业务元素确认操作所属frame。微应用可能有独立frame、路由和请求上下文；监听请求与表单操作使用已确认的上下文

优先用role/label及精确可见文本定位，再用稳定测试属性；页面没有语义定位条件时使用已观察到的稳定CSS。针对当前弹窗、抽屉或业务容器缩小范围，以真实成功/失败/空列表状态作为等待目标；不预设列表一定包含tbody，不以固定等待替代就绪判断。API名称和snapshot参数从当前工具契约取得

同一模块连续推进独立场景，身份与路由有效时复用已验证定位方案。导航、弹层变化或恢复后重新取得失效ref；操作被遮挡时先确认遮挡层及正确目标。请求监听在触发动作前建立，后续根据实际接口和scope核验

复杂控件先检查真实DOM及可编辑性：JSON编辑器可能使用原生textarea；选择器的combobox可能只读或被已选标签、清空按钮遮挡。按实际控件选择语义点击、稳定外壳或受支持的键盘操作，并核对选中状态与保存后回读。列表行可能显示普通文本而非链接，排序也可能由前端执行；断言使用页面实际展示与对应记录回读，而非把API数组顺序当作UI顺序

在既有探索记录或共享fixture中保存足够复现的信息：业务路由、frame的稳定URL模式/所属容器、关键定位器、就绪条件、请求匹配条件及verified_at/证据路径；去除URL中的凭据和敏感参数。下一轮先低成本验证再复用，局部失败只更新受影响部分。该信息不代替需求预期、身份检查或资源账本

## 本地自动化

认证文件按写集指定writer，复用项目配置加载器和secret引用；不相交角色fixture可并行，同文件互斥。缺项提供空模板与必要字段说明，不把凭据写入技能、代码、计划、回执或报告。

使用已批准的应用fixture，或让人工在Playwright控制的浏览器中登录一次。仅将必要状态保存到忽略目录（例如`test/.cache/auth/`），有效时复用。新状态必须在fresh headless context打开受保护页面，验证身份、适用的角色/组织与实际请求scope；在替代状态验证成功前保留仍工作的旧状态。

`storageState`不自动保存`sessionStorage`、任意内存状态或host注入认证。应用依赖这些状态时使用受限init script或受支持的应用fixture，按实际origin配置；拿到API token本身不能证明浏览器已登录。不要用ego-lite保持的登录掩盖Playwright自动化认证缺口。

若本地自动认证失败，人工状态只能作为明确的模式/方法变更单独记录与验证。保留原自动认证失败，不能将后续人工登录成功反写为自动认证成功。

## CI与自动bootstrap

对应写集writer按实际应用契约实现：加载配置→认证→核实身份及适用的组织/租户→建立Cookie/localStorage等必要状态→fresh context访问受保护页面→确认就绪与实际请求scope→持久化给依赖测试。

按应用处理过期、刷新、CSRF、origin及host注入。复用已授权的测试环境认证设施，包括应用提供的测试CAPTCHA配置；值来自本地secret或CI secrets。需要人工CAPTCHA、MFA或扫码时进入人工路径，不借E2E任务添加新认证绕过或改变生产认证。

Playwright setup project独立于自己生成的认证状态，业务project依赖该setup。正常运行保留依赖；`--list`只有发现意义，`--no-deps`不验证setup。按角色/worker分离状态路径，支持时限制文件权限。

在没有ego-lite或交互登录参与的fresh headless context验证bootstrap。只有exploration或人工本地能力时，可完成相应授权工作，同时报告无人值守自动化仍受阻。

## 测试身份与共享配置

应用无登录或开放访问时，跳过本节的身份准备，其余数据与配置规则照常适用。有认证时，优先复用已授权且满足场景的现有身份，包括预置受限账号和SSO身份，经项目结构化加载器读取；凭据保存在忽略文件或CI密钥中，只要求补充确实缺失且无法推导的输入。创建临时账号及应用实际具备的组织、角色和授权，或使用管理员身份做准备操作，只在应用支持、本轮需要并已授权时进行；管理员不作为普通UI回归的默认前置，也不代替受限身份验证。worker复用只读身份，授撤权等可变身份按case隔离，按最小权限准备。

应用有组织、租户、工作区等隔离层级时，当前上下文优先使用登录响应的默认值；可推导的根节点ID按项目真实初始化契约生成，并回读存在性、归属及启用状态。应用专属ID格式和权限码留在项目fixture及spec，通用模板仅记录约定。

专用测试范围按应用实际隔离层级确定，可以是独立环境、租户、组织、工作区或项目。用户确认后，spec只记引用名（如profile或配置key），真实ID、账号、手机号等放Git忽略的配置文件或CI密钥，确认证据存`test/.cache/`；之后复用此授权，不要求额外独占开关或重复填写ID。该范围内的共享配置是“既有业务数据只读”的唯一例外；没有已确认专用范围时，共享配置只读。变更前以真实身份和服务响应核对允许范围，按环境、范围、配置项建立冲突组并跨worker/进程互斥。Agent任务之间使用调度`conflict_keys`（如`shared-config:<环境>/<范围引用名>/<配置项>`）；Playwright worker之间优先不用锁：把会改同组配置的case放进单独project或文件串行执行（`workers: 1`或serial模式）。确需跨进程互斥时，由项目fixture在`test/.cache/`下使用平台适配的系统级文件锁，优先复用已有跨平台封装（POSIX可用flock，Windows使用等价内核锁）。进程退出释放内核锁；锁文件保持稳定、记录持有者，不随元数据原子替换，也不因时间过期而删除或替换仍被持有的锁文件；基于mkdir/mtime过期判定的锁库不满足此要求。

每次变更按[资源账本](contracts.md#resourcehumandelegation)登记`kind=shared_config`，脚本强制以下规则：intent必带`config_key`和`original_snapshot`（`test/.cache/`下的原值JSON）；变更成功后confirmed，owned=true，receipt为变更响应；恢复后回读存为JSON，absence_verified必带`restored_snapshot`，与原值的规范化JSON一致才接受。回读不一致时资源保持confirmed，残留建议为restore，并登记environment issue；同组后续写入先恢复回读再继续，不依赖进程退出后的锁表达未恢复。跨机器运行协调相同时段，或使用各方都能访问的锁。与其他使用方共用的范围（如共享系统租户、无隔离的单一环境）不变更配置；其中已授权的本轮专用数据仍按owned规则准备和清理。

恢复标记残留时由主Agent按[自动解阻](workflow.md#框架自修复与自动解阻)自行对账，不因存在标记就重复索要授权。在同组互斥下核对原账本绑定的身份、范围、配置项和不可变原值，取得当前服务回读；若已一致，直接将新回读作为`restored_snapshot`，通过resource事件登记`absence_verified`，不要求额外配置写入。项目fixture另有恢复标记时，在账本结案并验证恢复条件后用恢复入口归档，保留诊断历史。若尚未一致，只执行原授权包含的恢复；缺少可靠原值、归属或恢复权限时仍保留阻塞。

应用支持且本轮需要临时身份时，按run、worker或case登记intent及准确ID，先回读账号、适用的组织归属和完整有效权限，再提供独立登录态。业务资源清理完成后，只回收归属本run或其显式继承账本的临时身份及关联资源，按应用契约和依赖顺序选择实际支持且已授权的撤权、禁用或删除操作，并回读验证结果。支持删除时确认缺席后登记absence_verified，再释放唯一标识（如邮箱、手机号）及私有状态；不支持删除时按下节放回复用池或禁用，登记retained_verified，不伪造absence。旧会话即时失效仅在应用契约要求且本轮需要验证时检查，不把禁用、会话撤销或固定的“先禁用再删除”顺序作为所有应用的清理前置。中断恢复只处理原账本绑定的准确资源；未知创建结果阻止重建和猜测删除。准备核验、静态检查与真实权限回归分别报告。

### 临时身份复用池

应用不允许删除账号时，优先复用已有临时账号，不每轮新建。池在spec的`identity_pools.<池名>.max_accounts`声明上限；未声明的池不能使用。脚本维护跨run池账本`test/.cache/identity-pools.json`，只记账号引用名、状态、基线证据、租用run和隔离原因，不记密码。

- **创建**：resource intent带`pool`，仍走intent→confirmed并需创建授权。脚本统计池中账号及在途intent，含disabled和quarantined（仍占唯一标识），达到上限拒绝intent。首次用完后登记retained_verified：retention=pooled进入available，disabled直接记为disabled；pool_ref为账号引用名，池内唯一。record/finalize仅在本批新增带pool的retained_verified资源时，先同步当前run的池资源再提交state；普通issue、计时等事件不触发池扫描。pool-lease/pool-return仍全量补登记，历史状态不可读时保留占用/容量核算门禁。
- **密码**：不用可猜规律（如固定前缀加编号）。本地用每账号随机强密码，存Git忽略的凭据文件（如`test/.cache/auth/pool-credentials.json`，权限0600）；跨机器或CI用HMAC(种子, 账号引用名)推导，种子放CI密钥。事件、池账本和evidence只出现引用名。
- **租用**：每次runner调用使用独有的`--request ID`，`pool-lease --request ID --count N`一次租N个available账号；N必须覆盖该调用实际同时使用可变身份的全部槽位。不足返回`pool_exhausted`，不自动新建或复用他人租用。同一run/池/request以相同count重试时返回原分配；账本提交后派生文件写入失败也不额外租号。改变并发数或开始新调用须用新ID；部分归还后旧request不能重新租用。升级前无request的旧租用先按原run归还。
- **worker分配**：fixture为每个并发槽位固定编号`0..N-1`，使用前调用`pool-account --run RUN --pool NAME --request ID --workers N --slot K`，从权威账本核验并取得pool_ref；只读查询不要求主owner名，仍要求run未结束且租约有效，并在项目锁内核验。禁止取模或在账号不足时循环分配。重试worker沿用原槽位，不使用不断递增的worker编号。多project、分片或并行runner分别租用，或由fixture明确分配全局唯一槽位，不能假设各自从0开始的编号互斥。仍在执行的调用不能被相同request的另一个调用重复启动。Agent间仍以`identity:<池名>/<账号引用名>`作conflict_keys。
- **分配文件与核验**：`test/.cache/runs/<run>/identity-leases.json`为派生缓存，schema_version=2，`requests.<request>.pools.<池名>`含worker_count和按槽位排列的accounts；已归还项置null，其余槽位不前移。fixture不能只凭旧文件取号。取用前登录并回读角色、组织归属和权限，与基线一致才使用。密码过期或强制改密时在互斥内原子更新凭据文件；登录被拒不重复提交同一输入，按隔离归还。
- **归还**：`pool-return`批量提交，每个账号带回读evidence。先清理该账号产生的业务资源，再回读基线：一致为pooled；不一致且恢复失败为quarantined，须引用本run已登记的open environment issue；不再复用为disabled。
- **请求回收**：本run在某池的账号全部归还后，先把已用request ID存入该run的state.closed_pool_requests，再删除池账本中对应run的详细requests并刷新分配文件。旧ID仍拒绝重新租号，包括显式恢复旧run后；中断时归还可重试，不能先删记录再写已用ID。
- **执行顺序**：先完成prepare-regression或手工retention plan/apply，再租号并启动runner。租用、归还及请求回收都会改变池账本，使已生成但未apply的回收计划失效；不得跨这些操作保存旧计划继续删除。
- **隔离**：quarantined账号不再分配。产生隔离的run不能fully_accepted，只能以completed_with_failures或partially_blocked收尾；后续run不受阻，status列出隔离数并给出`resolve_quarantined_pool_accounts`。人工处理后用`pool-resolve`改为available（带基线回读证据）或disabled，并写明原因。
- **收尾**：本run仍有租用账号时，登记任何验收终态、finalize和new-round均被拒绝，须先归还。脚本只能约束已登记账号，fixture不得绕过账本直接建号或取号。

## 认证失败与敏感证据

分别诊断配置缺失、测试CAPTCHA拒绝、凭据拒绝、组织选择、组织不可用、状态过期、受保护页面失败、数据权限不足。保存脱敏错误、发生阶段、执行方法和恢复点。

凭据或配置被拒绝后，不重复提交同一失败输入；先等待用户修正或取得可观察的有效变更。技术网络等待也不能自动变成额外登录尝试。既有用户授权继续有效，无须为相同已授权操作重新求许可。

认证setup的trace、截图和video若会暴露凭据/token，应关闭。业务trace同样可能含敏感请求，按项目策略保存、限制访问和保留时间；报告仅引用路径及脱敏摘要。状态文件、密钥和完整trace不入Git。

## 人工等待与恢复

需要人工验证、身份选择、权限处理、业务澄清或明确审批时，worker先停止受影响动作，并立即持久化：

- 阻塞原因、需要的人类动作/决定、受影响case及任务
- 明确resume point、可观察的ready条件，以及是否需要显式approval
- 当前输入/baseline、页面路由与身份引用、代码与证据路径
- 创建intent、unknown/owned资源、清理进度和恢复方法

随后通知主Agent，由主Agent向用户说明具体动作与影响。请求、回复和resume信息使用契约字段，避免只写在对话。只让依赖该条件的任务等待，其他已授权且独立任务继续。

默认等待窗口300秒，轮询间隔15秒，可由项目/本轮配置调整；仅检查可观察就绪，不重复登录或产生额外业务写入。等待使用短而有界的调用，不为每次轮询续租；run默认租期3600秒，只有等待/操作确实可能超过剩余租期时才单独renew，不刷新来源marker。没有活跃操作可安全保存并释放配额，但不把仍进行的清理当作已结束。

若条件是人工在页面完成扫码/MFA等操作，可以在核实身份、权限和受保护页面就绪后恢复。若条件是明确业务审批或授权，页面可用、计时结束或任务无报错都不能代替用户明确批准。

超时仍未就绪时记录blocked、必要human request与安全恢复点，继续无关工作，并在下轮接管时重新检查来源、环境、身份与资源。清理可安全完成时按owned规则执行；暂不能清理则持久化残留，不能丢失账本。不要承诺没有实际调度机制的后台自动唤醒。

人工完成后先复核条件，而不是直接从旧browser ref继续。确认依赖变化是否使此前探索、认证、代码或执行结果失效，并只重新执行受影响部分。
