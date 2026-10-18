# 豆瓣Cookie保活 (DoubanCookieKeeper)

MoviePilot v2 插件：把豆瓣 Cookie 的「采集 → 校验 → 保活 → 分发 → 报警」串成一条自动链路，解决豆瓣插件隔三差五提示 Cookie 失效的问题。

## 它解决什么

豆瓣的登录态由服务端风控掌管，浏览器里长期不用、换 IP、多设备登录都会让 `dbcl2` 变成 `deleted`。一旦服务端作废，任何"定时访问"都救不回来，只能重新登录一次。

所以本插件不承诺"永不过期"，它做的是把过期带来的麻烦降到最低：

1. **自动采集**：从 CookieCloud 拉取 `douban.com` 的 Cookie（浏览器插件自动上传，无需手动复制）。
2. **准确判定**：不靠"能不能打开页面"，而是看豆瓣首页是否下发 `dbcl2="deleted"`、`/mine/` 是否返回 403 —— 这是豆瓣真正作废登录态的信号。
3. **低频保活**：每轮用有效 Cookie 访问首页 + 一个条目页，捕获豆瓣续期的 `ck`/`bid`，并保持会话活跃。
4. **自动分发**：把校验通过的 Cookie 写进 `DoubanFolio`、`DoubanDoulist` 等插件的 `cookie` 配置，并重载目标插件立即生效。**失效的 Cookie 绝不会写进去**。
5. **失效通知**：状态翻转时推送（可设置重复提醒间隔），并给出"重新登录 → 等 CookieCloud 上传"的处理步骤。
6. **可视化状态页**：来源、豆瓣 UID、Cookie 指纹、目标插件同步结果、最近 30 次检测记录一目了然。

## 安装

MP 插件市场添加仓库：`https://github.com/yuwancumian2009/MoviePilot-Plugins`，搜索「豆瓣Cookie保活」。

## 配置说明

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | |
| 失效/恢复时通知 | 开 | |
| 允许从 CookieCloud 拉取 | 开 | 需在 MP 设置里配好 CookieCloud |
| 低频保活请求 | 开 | 每轮访问首页 + 条目页，捕获续期 Cookie |
| 同步后重载目标插件 | 开 | 不重载要等 MP 重启才生效 |
| 检测/保活定时 | `0 */6 * * *` | 不要低于每小时，避免触发风控 |
| 同步目标插件 | `DoubanFolio,DoubanDoulist` | 逗号分隔，目标插件需有 `cookie` 配置项 |
| 失效重复提醒间隔 | 6 | 小时 |
| 手动 Cookie | 空 | 可选，留空则只用 CookieCloud / 缓存 |

## API

均需 MP API Token（`Authorization: Bearer <token>`）：

- `GET /api/v1/plugin/DoubanCookieKeeper/status`
- `GET /api/v1/plugin/DoubanCookieKeeper/check` — 检测并同步
- `GET /api/v1/plugin/DoubanCookieKeeper/pull` — 强制从 CookieCloud 刷新
- `GET /api/v1/plugin/DoubanCookieKeeper/sync` — 用缓存 Cookie 重新同步

## 注意

- 只做读请求，不做签到、不做写操作，最低风控风险。
- Cookie 明文保存在 MP 的插件配置/插件数据里，与其它豆瓣插件一致，请保护好自己的 MP 后台。
- 若判定有效但片单插件仍报写入失败，多半是豆瓣对 `ck` 的风控，可在插件页查看最近探测细节进一步排查。
