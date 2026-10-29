流程与字段说明见已安装技能ego-lite-playwright-e2e的SKILL.md

```json workflow-config
{
  "schema_version": 1,
  "scope": {"fixed": [], "modules": []},
  "source": [],
  "env": null,
  "authref": null,
  "data-policy": null,
  "commands": {},
  "concurrency": {"max_parallel_agents": "auto", "auto_parallel_cap": 4, "playwright_workers": 4, "browser_parallel_cap": 4},
  "regression": {"final": "all_existing_automation"},
  "retention": {"before_regression": true, "reports": "summary_and_unresolved_evidence"},
  "wait": {"timeout_seconds": 300, "poll_seconds": 15}
}
```

## 首次接入信息

由Agent按已安装技能的`references/onboarding.md`引导填写；能从工程发现的内容先复用，未确认项标“待确认”。本节只作说明，不新增配置块必填字段。受限URL、本地绝对路径和凭据放已忽略的本地配置，此处只写可公开的标识、引用与实际加载方式。

- 前端工程/工作区（仓库相对位置）与测试命令cwd：
- 权威需求来源标识、读取方式、版本与模块范围（具体映射填`source`）：
- 测试环境profile、页面入口加载方式与部署版本：
- 前端启动/测试/清理命令（复用项目脚本，配置填`commands`）：
- 后端代码库引用及版本（按需；可填“不提供/纯黑盒”）：
- 工具检查结论（Python、Node/包管理器、Playwright/浏览器、ego-lite集成、按需资料工具）：
- 首轮候选流程、用户选择与待澄清项（未选择不视为授权）：

## 身份与共享配置约定

本节进Git，只写引用名、加载入口和规则；不适用的项写“无”（如开放系统无登录、单租户系统无租户映射）。账号、密码、手机号、租户/组织ID、token等真实值放Git忽略的配置文件或CI密钥。

- 认证方式（无登录/表单/SSO等）与身份profile引用名：
- 专用测试范围（环境/租户/组织/工作区等）引用名及用户确认日期：
- 与其他使用方共用的范围及授权：
- 组织/租户等上下文的来源或推导规则：
- 凭据加载入口（`authref`）：
- 临时身份Bootstrap入口，worker复用与case隔离粒度：
- 不可删除身份的复用池：池名（上限写入配置块`identity_pools`）、密码来源（凭据文件/HMAC种子的密钥名）：
- 清理顺序与中断恢复入口：
- 共享配置冲突组key与锁位置：
