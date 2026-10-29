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

- Python 3.10+；本仓库脚本与单元测试仅依赖标准库，无需 `pip install`。
- Agent 环境中已安装并可使用 `ego-browser` 技能及其 ego-lite 浏览器集成；本仓库不分发该依赖。
- 目标项目已按自身要求安装 Node.js、Playwright、浏览器和其他测试依赖。
- 已确认的需求来源、测试环境、身份权限和专用 E2E 数据策略。

没有可用浏览器集成时，不把本地脚本自测当作页面探索或产品 E2E 通过。

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

Agent会引导提供需求文档地址/文件、测试环境、身份和数据边界，并按需询问后端代码库路径；WPS资料工具仅在读取相应来源时需要，详见[WPS 365 插件仓库](https://github.com/YiuTerran/dsh-wps365-plugin)。缺少Playwright、浏览器或ego-lite等依赖时会提示安装与验证，不自动下载或登录。随后梳理需求、请用户选择首轮范围，先跑通一个代表流程及清理，再扩展用例。

以下为 POSIX shell 示例；Windows 使用 `py -3` 和对应的 PowerShell 路径语法。

```sh
SKILL_DIR="$HOME/.codex/skills/ego-lite-playwright-e2e"
PROJECT="/path/to/target-project"
python3 "$SKILL_DIR/scripts/workflow.py" init --project "$PROJECT"
```

`init` 只补齐缺失的 `test/spec.md` 和 `test/plans/coverage.json`，不自动批准配置、不修改 `.gitignore`。
在目标项目中确认配置、固定范围、需求与用例映射，并将 `/test/.cache/` 加入该项目的 `.gitignore`，
再写入来源、认证或运行证据。需要只读诊断时使用：

```sh
python3 "$SKILL_DIR/scripts/workflow.py" doctor --project "$PROJECT"
```

完整执行顺序见 [SKILL.md](SKILL.md#最小命令顺序)，输入准备见 [工作流](references/workflow.md)。
不要将本仓库脚本的合成测试结果登记为目标项目的产品测试证据。

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
