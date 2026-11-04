---
status: accepted
---

# 内嵌 Agent 的 WebFetch 跳过域名预检，出站代理按端点可达性注入

内嵌 Agent 的 WebFetch 工具在抓取前会先向 `api.anthropic.com`（`/api/web/domain_info`）查询该域名是否在 Anthropic 的内容黑名单里，问不到就拒绝抓取，工具报 `Unable to verify if domain ... is safe to fetch`。这个端点写死在 CLI 内部，不跟随 `ANTHROPIC_BASE_URL`：把推理指向第三方网关的部署里，预检仍然打 Anthropic 官方域名。该域名在境内网络不可达，于是每一次 WebFetch 都止步于预检，Agent 侧表现为「网络检索不可用」。

另一条独立的问题在沙箱侧。沙箱内 Bash 的出站默认直连，被墙域名 TCP 超时，Agent 拿不到外部资料。claude-code 自带 HTTP 代理 mux：CLI 进程的 `HTTP_PROXY` / `HTTPS_PROXY` 是 mux 的上游，沙箱内的 curl 经 mux 转交给它。

决定：WebFetch 默认跳过域名预检，出站代理按端点可达性注入 CLI 子进程环境变量。`WebSearch` 仍不加入允许工具（`docs/adr/0069`），本决定不放宽服务端搜索。

## 规则

- 会话 options 构建时，settings JSON 带 `skipWebFetchPreflight: true`，除非 `ARCREEL_SKIP_WEB_FETCH_PREFLIGHT` 显式设为假值。保留预检时该键不写：CLI 的默认值就是「跑预检」。
- 跳过预检移除的是 Anthropic 的内容黑名单与「预检不可达即拒」的 fail-closed，不是宿主网络边界。Agent 早已通过 Bash 沙箱拿到全域与 loopback 出网（`allowedDomains: ["*"]` 加 `allowLocalBinding: true`，见 `docs/adr/0069`），沙箱内 curl 本就能直连宿主任意端口。
- 出站代理来源按优先级取：`ARCREEL_AGENT_PROXY_URL`，然后 `HTTPS_PROXY`、`https_proxy`、`HTTP_PROXY`、`http_proxy`、`ALL_PROXY`、`all_proxy`。没有候选代理时不注入任何代理变量。
- 候选代理的 host:port 必须 TCP 可达（只确认端口有人接，不发代理协议字节）。可达时把代理变量统一指向该 URL；不可达或 URL 不合法时把这些变量置空，CLI 侧按「无代理」处理，回退直连。置空而非删键，是因为 SDK 把 `options.env` 叠加在继承来的进程环境之上，删键不撤销继承值。
- `no_proxy` 合并三处条目：宿主环境的既有条目、`ARCREEL_AGENT_NO_PROXY` 的条目、本地地址默认项（`127.0.0.1`、`localhost`、`::1`、`*.local`、`host.docker.internal`）。本地地址默认项不可被操作者的条目挤掉。
- socks 类代理只进 `ALL_PROXY` / `all_proxy`：CLI 自身的 HTTP 客户端不会说 socks 协议，写进 `HTTP_PROXY` 会让它的请求直接失败。
- 域名边界仍由 `agent_access_policy.build_sandbox_settings` 的 `network.allowedDomains` 决定，本决定不改这一层。本部署取全域放行，两条代理注入路径（CLI 进程环境变量与 sandbox `network.httpProxyPort`）都会让出站绕过 CLI 自身的域名白名单，全域放行已包含该边界。
- 代理 URL 的 userinfo 随 CLI 子进程环境进入沙箱 Bash 可见范围，代理口令不写进 `ARCREEL_AGENT_PROXY_URL`。

## Considered Options

- **无条件写死 `skipWebFetchPreflight: true`，不留开关**：少一个旋钮，但恢复预检只能改代码。env 开关让部署者能在 `api.anthropic.com` 可达时把预检装回来，代价只是多一个变量。
- **把预检开关与代理写进物化的 settings 文件**：settings 的路径与取值按会话变化（项目记忆目录已经因此走过 JSON 串这条路，见 `docs/adr/0074`），落盘会与 profile manifest 的「未改内置文件」判定打架。
- **用 sandbox `network.httpProxyPort` 把沙箱流量桥到本机代理端口**：这条旁路不经 CLI 的 mux，绕开 CLI 自身的域名白名单；且只覆盖沙箱流量，CLI 自身的 WebFetch 抓取仍要另配代理。走 CLI 进程环境变量一条路，WebFetch 与沙箱出站共用同一个上游。
- **只注入代理、不做端点探测**：宿主可能配了代理地址却没有代理进程在跑，死代理会让 WebFetch 与沙箱出站一起报 `CONNECT tunnel failed, response 502`，比直连超时更难定位。
- **不动出站，让 Agent 继续直连**：被墙域名永远拿不到资料，Agent 的检索能力在境内网络下形同虚设。
- **放开 `WebSearch`**：`docs/adr/0069` 明确排除，本次不推翻。服务端搜索与第三方网关的契合度要单独评估。

## Consequences

- WebFetch 恢复可用；代价是被抓取的域名不再上报 Anthropic，Anthropic 的内容黑名单不再对该部署生效。
- Agent 的出站经本机代理，境内网络下能取到外部资料。代理存活时 CLI 自身对 Anthropic 网关的请求也经代理，需要直连时把网关主机名加进 `ARCREEL_AGENT_NO_PROXY`。
- 代理不可达时表现为「回退直连」而不是「出网全断」，但回退直连取不到被墙域名——这是「代理没起」与「Agent 不联网」两种状态的区别，排障时看 CLI 子进程环境里的代理变量是空值还是 URL。
- 容器部署时宿主代理不在容器的 loopback 上：代理地址要写成容器可达的宿主地址（如 `host.docker.internal`），且宿主代理须监听该地址可达的网卡。
