# UI覆盖设计与失败诊断

新增UI用例或补齐获准模块时读取本页；已有回归和按报告修复不要求重新建立计划。需求决定预期，ego-lite观察页面及实际角色/状态，必要时定点阅读前后端代码；页面现状不自动成为正确预期。UI与API复用业务case，分别绑定当前suite的实现和结果，详见[套件与共享用例](suites-and-cases.md)。

## 覆盖计划与校验

以[UI计划模板](../assets/ui-coverage.template.json)建立项目侧`test/plans/ui-coverage.json`。模板是带真实缺口的示例片段，替换来源、版本、指纹、页面及case，不把示例文字当作来源证据。来源可包含需求、页面探索及相关实现观察，`locations`指向实际可追溯资料；敏感地址、身份、动态资源ID留在受控配置和证据中。

计划字段：

- `schema_version=1`、`suite_id`：当前UI套件。
- `sources`：`source_id/kind/revision/fingerprint/locations`；`inventory`：盘点方法、已获准的`modules`、纳入的页面ID及有原因的排除项。`pages`必须与纳入清单一致；这不是全应用自动路由发现，也不证明范围已获授权。
- 页面：`page_id/module/route_key/source_ids`、`scenarios`和`review`。`route_key`是静态业务标识，不保存带身份或资源参数的实际URL。
- 场景：全局唯一`scenario_id`、`source_ids`、`dimensions`、`expected`、非空`assertions`及`case_ids`。不同独立场景使用不同case，同一场景可同时证明几个维度；project/retry/repeat不是额外业务case。

每页评审下列维度，不默认穷举所有组合：

| 维度 | 核对内容 |
| --- | --- |
| `main_flow` | 主要动作、成功条件及准确资源关联 |
| `validation_errors` | 必填、边界、非法输入、请求/业务失败反馈，以及不应发生的副作用 |
| `loading_empty` | 实际存在的加载、空列表、无结果及失败状态 |
| `persistence` | 刷新、退出后重新进入，必要时独立回读 |
| `roles_permissions` | 应用真实角色的可见、可读、可编辑和拒绝行为 |
| `filter_sort_pagination` | 实际控件、结果正确性、状态重置及有风险的交互组合 |
| `keyboard` | 需求适用的焦点、键盘操作及可达性 |
| `viewport` | 需求支持的布局及关键交互 |
| `visual` | 已获准且可稳定判定的视觉状态，不默认建立截图基线 |

每项`review`使用`covered / blocked / not_applicable`。`covered`必须列`scenario_ids`，且与场景的`dimensions`一致；`blocked`说明尚缺什么，可以引用已完成的部分场景；`not_applicable`说明依据，不能同时引用场景。Agent核对语义，不为填满矩阵扩大原授权，也不以随意填写“不适用”消除真实缺口。

在新增实现就绪后，项目runner派发前显式调用：

```sh
python3 <skill>/scripts/ui_coverage.py \
  --project <frontend> \
  --plan <frontend>/test/plans/ui-coverage.json \
  --catalog <catalog-root>/test/plans/catalog.json \
  --coverage <frontend>/test/plans/coverage.json \
  --suite-id frontend \
  --discovery <frontend>/test/.cache/discovery.json
```

`--discovery`接收当前工程、相同配置下完整的Playwright `--list --reporter=json`输出。映射按spec真实路径、准确title及可选project匹配；省略project允许同一声明的多个原生实例，显式空字符串只匹配未命名project。不同case不能重复使用同一个test声明或`test_id`。实际发现出现同文件同title的多个声明时，应修订明确命名，不能猜测归属。

只校验计划内场景，不要求无关模块或其他suite同时补计划；共享case的API实现不能满足UI映射。spec必须存在于当前工程，case须为active且要求UI实现，映射须为本suite的有效automation。未提供discovery时输出`discovery=not_supplied`，只验证声明，不声称测试已被实际发现。

退出码`0`为声明检查通过，`1`为结构/引用/映射错误，`2`为存在已声明缺口。`--allow-known-gaps`仅让已知缺口返回0，输出仍为`known_gaps`；不消除缺口、接受测试结果或改变验收门禁。完整性语义、来源有效性和真实执行仍需分别核实。此入口不自动接入已有runner，也不作为所有历史UI回归的新前置。

## 可复用探索操作契约

延续[页面定位与探索复用](browser-and-auth.md#页面定位与探索复用)，在既有模块设计/探索记录或共享fixture旁记录下列内容，不为每次点击新增缓存或交接文件：

| 内容 | 示例 |
| --- | --- |
| 来源与起始条件 | 需求章节；已核实的身份引用、订单页、可写状态 |
| frame与容器 | 业务frame的稳定特征；当前“新建订单”弹窗 |
| 语义目标与动作 | 按label填写输入，按role点击保存；先建立请求监听 |
| 可观察变化 | 当前弹窗关闭且出现准确成功状态；失败时错误可见、输入保留 |
| 业务断言 | 创建回执ID对应的列表行及字段正确，刷新/重新进入后仍匹配 |
| 资源义务 | 创建前intent、成功回执及准确ID、清理顺序、absence回读入口 |
| 复用依据 | verified_at及原有证据路径，哪些条件变化会使该契约失效 |

动作成功返回、机械回放结束或元素存在，均不能代替业务断言。定位失效时重新确认frame、遮挡和真实控件，修订受影响部分；不能用相似目标猜测、自愈跳过操作或弱化断言，把原回归失败改成通过。

恢复时记录已执行前缀及不确定写入。保存已点击但响应未知时，先按原身份、scope、账本与准确关联回读；无法确认是否提交就保持该写入受阻，不再次点击保存或猜测删除。其他独立场景可继续，资源对账沿用原有[清理契约](cleanup.md)。

## 可选失败诊断fixture

[ui-diagnostics.mjs](../assets/ui-diagnostics.mjs)导出`withUiDiagnostics(baseTest, options)`。从项目已有的`test`扩展，再导出给需要诊断的UI spec；保留现有fixture，不引入第二份Playwright依赖。复制到项目测试辅助目录或以稳定的本地路径导入，列入代码指纹读集。

```js
import { test as projectTest, expect } from './project-fixtures.mjs'
import { withUiDiagnostics } from './ui-diagnostics.mjs'

export const test = withUiDiagnostics(projectTest, {
  routeKey(url) {
    return new URL(url).pathname === '/orders' ? 'orders.list' : undefined
  }
})

test('ORDER-CREATE create order', async ({ page, uiDiagnostics }) => {
  await uiDiagnostics.step('orders.open', async () => {
    await page.goto('/orders')
    await expect(page.getByRole('heading', { name: 'Orders', exact: true })).toBeVisible()
  })
})
```

`uiDiagnostics`是依赖当前`context`的自动test fixture，启用它会准备浏览器上下文，故只用于UI suite；API suite继续导入自己的原有fixture。监听已有页面及新页面，记录page error、request failure及HTTP 400–599元信息，不采集console原文。额外手工创建的独立context不在当前context监听范围内。

`step(id, operation, {phase})`通过原生`test.step`执行，返回原结果，失败时抛出原错误；`phase`为`test`（默认）、`cleanup`或`recovery`。ID及可选`routeKey`返回值是项目定义的静态标识，仅允许字母开头、字母数字及`_.:-`、最长80字符；格式约束不证明内容已脱敏。`routeKey`必须同步，未知地址返回undefined，不能返回完整URL、资源ID、账号或秘密。页面事件的`active_step_id`只是当时唯一活跃步骤，不证明因果；并行步骤标记`concurrent_steps`，不猜归属。

默认不记录URL、path、query/hash、错误message/stack、header/cookie、请求/响应body、DOM、页面文本或截图。原生trace、截图、日志及项目其他fixture仍按项目显式配置独立管理；本组件不会清除其中敏感信息，也不承诺通用脱敏。需要丰富证据时由项目明确配置范围、访问控制及掩码。

每个原生Attempt默认最多200条事件、64 KiB，`maxEvents/maxBytes`可调低，不能超过这两个上限，最小字节预算1024。超限计入`dropped_events`；失败步骤结束事件优先替换普通事件，并保留已经记录的较早失败。原生错误本身仍在原生报告内，诊断附件不复制错误文本。

失败/超时/中断或fixture结束前已经可见的原生错误，会通过`testInfo.attach`附加`ui-diagnostics` JSON，含步骤、受限网络事件、原生错误数量、截断信息及`completeness=best_effort_until_fixture_teardown`。成功用例即使出现预期4xx或被业务捕获的错误也不因此变为失败，不生成失败附件。采集、回调和附件写入异常不能替代原始测试或清理错误。

清理步骤可用`phase: 'cleanup'`记录，同时遵循原账本、原授权和项目对多重异常的保留方式；本组件不执行清理，也不能修复项目`finally`本身覆盖原错误的写法。需要采集清理fixture异常时，使它显式依赖`uiDiagnostics`，保证它先结束、诊断fixture后收尾。依赖context初始化之前的失败、强制杀进程及更晚的teardown错误可能没有附件；附件缺席不表示这些阶段成功。退出移除所有自身监听器。

附件使用原生JSON/blob/HTML及原有内容哈希校验，不增加结果权威来源。诊断步骤、4xx/5xx及浏览器事件只提供线索，失败分类仍按[报告诊断](reports.md)核对需求、测试代码、实现与实际部署。
