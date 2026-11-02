# Ego-lite + Playwright E2E Skill

将需求、真实 Web 页面探索、独立 Playwright 用例和回归证据连起来的 Agent 技能。
技能名为 `ego-lite-playwright-e2e`，入口是 [SKILL.md](SKILL.md)。

适用于需求驱动的 Web E2E 覆盖补全与回归验收；不用于纯 API/单元测试、一次性页面检查或直接修复业务代码。

## 能力与边界

- 核对需求、计划、coverage 和实际独立测试之间的覆盖缺口。
- 使用 ego-lite 探索真实页面，再编写与已审核预期对应的 Playwright 测试。
- 以本地 CLI 管理来源快照、审批证据、运行租约、共享配额和资源清理记录。
- 绑定每次 Playwright CLI 的原始 JSON、blob 和 HTML，核验全轮合并报告，保留失败、重试和未执行结果。
- 只自动回收安全白名单内的本地产物，不删除业务数据；业务代码修改需另行授权。

脚本只管理本地状态和证据，**不会启动 Agent、浏览器或产品测试**。安装技能不等于获得目标系统的访问或写入授权。

## 前置条件

- Python **最低 3.10**；本仓库脚本与单元测试仅依赖标准库，无需 `pip install`。所有辅助命令启动时检查实际解释器，版本过低直接退出并显示版本和路径；同一流程固定使用已验证的解释器，不能只凭 `python3` 命令名判断版本。
- 执行已有自动化时，目标项目按自身要求具备 Node.js、Playwright、浏览器及必要夹具；直接回归无需 ego-browser 或需求来源读取工具。
- 新增页面探索默认使用 `ego-browser` 技能及 ego-lite 集成；本仓库不分发该依赖。资料读取工具仅在核验对应来源时需要。
- 按当前阶段确认需求预期、测试环境、身份权限和 E2E 数据边界；已有回归沿用既有预期。

依赖安装和浏览器下载复用已有有效授权，并遵守宿主文件与命令审批；仅缺少授权或需人工参与时请求用户。本地脚本自测不证明页面探索或产品 E2E 通过。

回归执行环境须允许在项目约定目录创建产物，并保留正常文件系统语义；兼容的受管 shell 也可使用。`preflight`自动探测 Python 文件操作，可按输入检查实际 Node；`doctor`保持只读。若报告 `fs_mkdir_not_idempotent` 或产物目录权限错误，先通过宿主支持的授权、修复或宿主终端解决，再在相同运行环境和实际产物父目录复检。技能不会剥离环境变量或更换产物根目录绕过限制；详见[运行前提与兼容性](references/onboarding.md#运行前提与兼容性)。

## 安装与调用

将整个仓库克隆到 Agent 的技能目录，目录名使用技能名，不要只复制 `SKILL.md`。
以 Codex 默认的用户技能目录为例（已设置 `CODEX_HOME` 时使用其下的 `skills/`）：

```sh
git clone https://github.com/YiuTerran/agent-ui-test-skill.git \
  "$HOME/.codex/skills/ego-lite-playwright-e2e"
```

其他支持目录式 `SKILL.md` 的 Agent 使用其对应的技能目录；浏览器工具和授权机制仍须在该环境实际验证。
也可以保留当前 checkout，通过绝对路径引用其中的 `SKILL.md`，避免维护两份源码。

如果当前 Agent 支持安装技能，也可以直接告诉它：**“请从 `https://github.com/YiuTerran/agent-ui-test-skill` 安装 `ego-lite-playwright-e2e`。”**
安装后可继续询问“这个 skill 怎么用？”，或让 Agent 在目标前端工程目录按首次接入引导开始配置。

调用示例：

```text
使用 $ego-lite-playwright-e2e 检查当前项目的需求覆盖缺口，
先探索缺失场景并提交用例预期供我审核，再补 Playwright 测试并执行回归。
既有业务数据只读，仅使用已授权的专用 E2E 资源。
```

## 首次接入目标项目

建议在目标**前端工程目录**调用本技能，让Agent按[首次接入引导](references/onboarding.md)识别工程、补齐配置并检查依赖。monorepo沿用实际E2E工作区；纯黑盒项目也可使用独立测试工程。

Agent会复用已有输入，集中补充需求来源、测试环境、身份和数据边界；后端源码可不提供。WPS工具仅在读取相应来源时需要，详见[WPS 365 插件仓库](https://github.com/YiuTerran/dsh-wps365-plugin)。按阶段检查依赖，沿用已有安装授权及宿主审批；人工认证只阻塞相关任务。

用户已明确要求补齐指定需求/模块内全部缺口时，直接以原始指令和证据登记当前清单；同一授权内的新缺口更新登记，范围不明确或越界时才征询。同一任务的技术新run核实授权仍有效后，以当前清单和原始授权证据重新登记，不自动继承旧事件或旧任务“仅本轮”的选择，见[授权复用与自动推进](references/workflow.md#授权复用与自动推进)。先验证代表流程及清理，再持续推进获准目标。

以下为 POSIX shell 示例；Windows 使用 `py -3` 和对应的 PowerShell 路径语法。

```sh
SKILL_DIR="$HOME/.codex/skills/ego-lite-playwright-e2e"
PROJECT="/path/to/target-project"
python3 "$SKILL_DIR/scripts/workflow.py" init --project "$PROJECT"
```

`init` 只补齐缺失的 `test/spec.md` 和 `test/plans/coverage.json`，不自动批准配置、不修改 `.gitignore`。
工作流元数据固定在这些文件和 `test/.cache/`；已有测试、fixture、Playwright配置及命令cwd沿用项目布局，以coverage的spec/plan、真实发现及Attempt的cwd绑定，`test/src/`仅为新工程建议。直接回归无需初始化；调度请求的[config_path](references/contracts.md#双预算与就绪任务)可指定项目相对Markdown路径，或显式null跳过无关的test/spec.md，默认仍读取该文件；规范配额账本和残留索引保持同一PROJECT。
在目标项目中确认配置、固定范围、需求与用例映射，并将 `/test/.cache/` 加入该项目的 `.gitignore`，
确认缓存无文件进入Git索引后，再写入来源、认证或运行证据。`doctor`同时检查忽略规则和索引状态：

```sh
python3 "$SKILL_DIR/scripts/workflow.py" doctor --project "$PROJECT"
```

完整执行顺序见 [SKILL.md](SKILL.md#最小命令顺序)，输入准备见 [工作流](references/workflow.md)。
不要将本仓库脚本的合成测试结果登记为目标项目的产品测试证据。

长回归优先复用项目已有恢复能力；[checkpoint sidecar](references/checkpoints.md#一次接入项目runner)是仅适用于retry=0、repeatEach=1的可选单次结果适配器。保留原生retry/repeatEach设置，不适用时走原生采集，配额、冲突组及残留门禁照常执行，不宣称逐case断点复用。

## 仓库结构

```text
SKILL.md            技能入口与安全门禁
references/         工作流、JSON 契约、报告绑定、浏览器与认证
assets/             配置与 coverage 模板、事件 schema
scripts/            本地状态、配额、来源缓存、回收与报告核验工具
scripts/tests/      合成数据单元测试，不访问真实产品或浏览器
.github/workflows/  本地工具的跨平台单元测试配置
```

## 开发验证

从仓库根目录运行：

```sh
python3 -m unittest discover -s scripts/tests -v
```

CI 配置覆盖 Linux、macOS、Windows，以及 Python 3.10 和 3.13。
平台专属测试可能跳过；非 Windows 的通过不能证明 Windows 原生句柄、junction 或进程行为正确。
CI 仅验证本地工具，不验证 ego-lite、Playwright 版本兼容性、真实登录或产品业务行为。

## 公开仓库与隐私

- 本仓库只收录通用技能、工具、空模板和合成测试数据；测试中的凭据形状字符串仅用于验证脱敏与拒收行为。
- 不提交真实需求快照、审批记录、内部地址、账号、Cookie、token、`storageState`、trace、截图或运行报告。
- 目标项目的临时状态、认证文件和大产物统一存入已忽略的 `test/.cache/`；仅保存必要的引用与脱敏摘要。
- `.gitignore` 只是防误收录措施，不会清除已跟踪文件或历史中的秘密；公开前仍须审核拟提交内容。
- 如需引入真实项目样例、第三方素材或受限集成文档，先确认脱敏和公开权限。

本仓库当前未指定开源许可证；公开可见不等于授予开源许可，分发或复用前请确认授权。
