# IP 池使用说明

## 一句话结论

IP 池支持两种供货方式：**代理服务商 API 自动拉取**（推荐）和**自备 IP 名单**。
接上 API 后整条链路是：调 API 拉 10 个 IP → 检测通过 9 个 → 逐页轮换抓取 → 80 条入库、零字段缺失。

---

## 标准用法

```bash
python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy-api --pages 3 --csv
```

`--proxy-api` 会自动完成：**调 API 拉新 IP → 写 ip_list.txt → 逐个检测 → 剔除死 IP → 轮换抓取**。
池子里的 IP 被抓空时还会自动再拉一批，不需要重开命令。

| 调试参数 | 作用 |
|---|---|
| `--pages 0` | 抓到没有房源为止（上限 20 页） |
| `--proxy-rotate random` | 改成随机取 IP（默认 `round` 顺序轮换） |
| `--no-proxy-check` | 跳过建池检测，全部直接进池（快，但会带着坏 IP 跑） |
| `--verbose` | 打印每页用了哪个 IP、HTTP 状态码 |
| `--proxy-file xx.txt` | 不用 API，改读指定名单文件 |

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `瓜子二手车_dg.py` | 主程序。IP 池相关代码集中在 `fetch_html_requests` 与 `build_ip_pool`（搜 `IP池` 定位） |
| `ip_pool.py` | IP 池内核：检测 / 轮换取 IP / 连续失败 3 次剔除 / 池空自动补货 |
| `proxy_api.py` | 调服务商 API 拉 IP，兼容 txt 与 json 两种返回格式 |
| `api_url.txt` | **你的 API 链接**（含 secret，不要外传、不要提交到仓库） |
| `ip_list.txt` | 当前 IP 名单，由 `--proxy-api` 自动覆盖写入 |
| `tools/verify_exit_ip.py` | 验证流量确实走了代理（对比本机出口 IP 与各代理出口 IP） |
| `tools/diag_proxy.py` | 代理分层体检：区分「TCP 都不通」和「能连但转发不了 https」 |
| `tools/local_proxy.py` | 本地自建代理，没有可用 IP 时用它验证代码链路 |

---

## IP 池是怎么工作的

```
api_url.txt ──请求──▶ 代理服务商 API ──▶ 10 个 ip:port
                                          │
                                          ▼
                              写 ip_list.txt，逐个真发请求检测
                                          │
                          ├─ 200 且页面含 car-box ──▶ 进池
                          └─ 超时 / 拒绝 / 被拦 / 兜底页 ──▶ 丢弃

抓取：第 1 页 → 池里第 1 个 IP
      第 2 页 → 池里第 2 个 IP     （--proxy-rotate round 顺序轮换）
      第 3 页 → 池里第 1 个 IP
      某页失败 → 换下一个 IP 重试（最多 4 次）
      同一 IP 连续失败 3 次 → 踢出池子
      池子被踢空 → 自动再调一次 API 补货（最多 5 轮）
```

### 两个关键经验

1. **只判「能连上」不够。** 检测必须对目标站真发一次请求，并检查页面里有没有关键元素（`car-box`）。
   否则被风控返回的兜底页也会被当成「可用」，抓回来全是 0 条。
2. **短效代理存活期是分钟级。** 实测拉取后 7~9/10 可用，**8 分钟后只剩 3/10**。
   所以每次运行都要重新拉，不要复用上次的名单。

---

## 实测数据（参考）

| 环节 | 结果 |
|---|---|
| API 返回 | 10 个 `ip:port`，纯文本一行一个 |
| 检测通过 | 9 / 10（挂掉的多为「读取超时」） |
| 抓取 2 页 | 80 条，8 字段零缺失 |
| 两页用的 IP | 两次抓取分别命中不同的 IP，轮换确认生效 |
| 出口 IP 归属 | 本机出口与各代理出口分属不同省份，说明流量确实经代理转发 |
| 存活衰减 | 拉取后立即测 7~9/10 可用；8 分钟后降到 3/10 |

---

## 浏览器引擎怎么挂 IP 池

代理挂在**浏览器上下文**这一层，不在浏览器进程上：

```python
ctx = browser.new_context(proxy={"server": "http://ip:port"})
```

换 IP 就是关掉旧上下文、用新代理开一个新的 —— 浏览器进程不重启，实测零点几秒。
旧上下文的 Cookie 会被搬到新上下文，已经过掉的滑块验证不会因为换 IP 而作废。

两套引擎都能挂池，区别只是写法：

| 引擎 | 代理挂在哪 |
| --- | --- |
| requests | `session.get(..., proxies={...})` |
| playwright | `browser.new_context(proxy={...})` |

实测：浏览器模式下每页都命中不同 IP；第 1 页前两个 IP 失败后自动换到第三个，成功抓到 40 条。

---

## 两条诊断命令

```bash
python proxy_api.py --check              # 单独拉 IP + 检测，看这批质量如何
python tools/diag_proxy.py               # 对 ip_list.txt 里的 IP 做分层体检
python tools/diag_proxy.py 1.2.3.4:8080  # 也可以直接指定要体检的代理
```

`tools/diag_proxy.py` 把问题分成两层：① TCP 端口通不通、② 能不能转发 https。
第一层就挂说明 IP 已死；第二层挂多半是代理不支持 CONNECT 隧道。

---

## 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `读取超时` / `代理连接失败` | 该 IP 已死 | 正常损耗，池子会自动剔除并补货 |
| `HTTP 407` | 代理需要账号密码 | 名单里写成 `用户名:密码@ip:端口` |
| `页面无 car-box` | 能通但被反爬拦了 | 拉长页间延时；用 `--verbose` 看最终 URL |
| `API 没返回任何 IP` | 订单过期 / 提取次数用完 / 未绑白名单 | 去服务商后台查订单状态 |
| `cssselect does not seem to be installed` | 缺解析依赖 | `pip install cssselect` |
| `IP池构建失败，改为直连` | 建池时 0 个可用 | 程序自动降级直连，不会中断任务 |

---

## 安全提醒

`api_url.txt` 里的 `secret` 等同于账户凭据，拿到它就能消耗你的提取次数。
该文件已在 `.gitignore` 中被排除，但自己也要注意不要截图外发。更稳妥的做法是改用环境变量：

```bash
# Windows（当前窗口临时生效）
set PROXY_API_URL=http://route.xiongmaodaili.com/xiongmao-web/api/glip?...
```
