# 瓜子二手车数据采集器 · Guazi Used-Car Spider

一个面向**学习与实践**的二手车数据采集项目：双引擎（浏览器自动化 / HTTP 接口）自由切换，
内置可插拔 IP 池，一条命令完成「筛选 → 翻页 → 解析 → 落盘」。

> 仅用于个人学习与技术研究，请遵守目标网站的 robots.txt 与服务条款，控制请求频率，不要用于商业用途。

---

## 目录

- [项目特性](#项目特性)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [双引擎对比](#双引擎对比)
- [输出说明](#输出说明)
- [项目结构](#项目结构)
- [IP 池是怎么工作的](#ip-池是怎么工作的)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 项目特性

| 能力 | 说明 |
|---|---|
| **双引擎** | `--engine playwright` 用真实浏览器翻页；`--engine requests` 直接 HTTP 请求，速度快 10 倍 |
| **浏览器优先** | 默认走浏览器：页面筛选条件由你手动点选，程序照抄地址栏，不猜 URL 规则 |
| **反爬处理** | 随机延时、失败重试、兜底页识别、验证页识别、浏览器 Cookie 复用 |
| **IP 池** | requests 引擎可挂代理池，每页换 IP、坏 IP 自动剔除、池空自动补货 |
| **结构化输出** | 8 个字段的 Excel / CSV，字段清洗、价格解析、去重全部自动完成 |

---

## 环境要求

- Python **3.9+**（开发与实测环境为 3.14.7）
- 首次运行浏览器引擎需下载 Chromium（约 150 MB）

---

## 安装

```bash
# 1) 安装 Python 依赖
pip install -r requirements.txt

# 2) 安装浏览器内核（只跑 requests 引擎可跳过）
python -m playwright install chromium
```

> **下载太慢怎么办？** 程序内置了浏览器自动回退：自带 Chromium 找不到时，会自动改用系统已装的
> Microsoft Edge 或 Google Chrome 启动，不需要下载 150 MB 的内核。
> 只有三者都没有时才会报错。

若 `pip` 下载慢，可换国内镜像：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 快速开始

### 模式 A：浏览器自动化（默认，推荐）

```bash
python 瓜子二手车_dg.py
```

运行后会：

1. 弹出一个**可见的** Chromium 浏览器，停在瓜子东莞列表页；
2. 你自己在页面上点城市 / 品牌 / 价格 / 年份等筛选条件；
3. 回到终端按 <kbd>Enter</kbd>，程序读取当前地址栏作为抓取起点；
4. 浏览器**不关闭**，在同一个会话里逐页翻取（Cookie、设备指纹全部保持）；
5. 抓完自动保存 Excel，然后关闭浏览器。

> 为什么推荐它：瓜子的筛选是「页面跳转 + JS 渲染」出来的，URL 规则并不统一
> （实测 `/dg/bmw/price6/` 有效，而 `?price=5,10` 会被忽略）。让你在浏览器里点一遍、
> 程序直接照抄地址栏，是最不容易拼错的方式。

### 模式 B：requests 接口（快，不开浏览器）

```bash
python 瓜子二手车_dg.py --engine requests --no-manual --city dg --pages 5
```

不弹窗、速度快，适合批量采集。但同时更容易触发风控，建议配合延时与重试使用。

### 模式 C：requests + IP 池（大规模采集）

```bash
# ① 用代理服务商 API 自动拉 IP（推荐）
python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy-api --pages 20 --csv

# ② 用自己准备的 IP 名单
python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy --proxy-file my_ips.txt
```

`--proxy-api` 会自动完成：**调 API 拉 IP → 逐个检测 → 剔除死 IP → 轮换抓取 → 池空自动补货**。

> 搭配 IP 池时，必须加 `--engine requests`。浏览器模式下整段会话固定一个出口 IP，
> 中途换 IP 要重启浏览器，代价大于收益。

---

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--engine` | `playwright` | 抓取引擎：`playwright` / `requests` |
| `--pages` | `3` | 抓取页数，`0` = 抓到没有房源为止（上限 20 页） |
| `--manual` / `--no-manual` | `--manual` | 是否在浏览器里手动点筛选。默认手动 |
| `--headless` | 关 | 无头模式（仅 `playwright` + `--no-manual` 生效） |
| `--city` | `dg` | 城市拼音路径：`dg` 东莞 / `gz` 广州 / `sz` 深圳 / `fs` 佛山 |
| `--brand` | 无 | 品牌路径段，如 `bmw`（仅 `--no-manual`） |
| `--price-min` / `--price-max` | 无 | 价格区间（万），自动映射到最近的价格档位，抓完再本地精确过滤 |
| `--price-code` | 无 | 直接用瓜子价格档位 `0~15`，如 `6` = 5 万左右 |
| `--extra-query` | 无 | 通用 query 透传，如 `"year=2018-2020&fuel=汽油"` |
| `--csv` | 关 | 额外导出一份 CSV |
| `--no-fallback` | 关 | 关闭「requests 彻底失败时临时开浏览器救这一页」 |
| `--verbose` | 关 | 打印 HTTP 状态码、HTML 长度、重试过程、当前用的哪个 IP |
| `--proxy` | 关 | 启用 IP 池（读 `ip_list.txt`，先检测再用） |
| `--proxy-file` | `ip_list.txt` | 指定代理名单文件，一行一个 `ip:port` |
| `--proxy-api` | 关 | 建池前先用服务商 API 拉一批新 IP（链接放 `api_url.txt`） |
| `--proxy-rotate` | `round` | `round` 顺序轮换 / `random` 随机取 IP |
| `--proxy-workers` | `10` | IP 并发检测线程数 |
| `--no-proxy-check` | 关 | 跳过建池检测，全部 IP 直接进池（快，但会带着坏 IP 跑） |

---

## 双引擎对比

| 维度 | `playwright`（浏览器） | `requests`（接口） |
|---|---|---|
| 反爬对抗 | **强**，真实浏览器指纹、Cookie、JS 全执行 | 弱，靠请求头与频率伪装 |
| 速度 | 慢，每页 2~5 秒 | **快**，每页 < 1 秒 |
| 依赖 | 需下载 Chromium | 仅需 Python 库 |
| 可挂 IP 池 | ❌ 会话固定出口 IP | ✅ 每页可换 IP |
| 适合场景 | 少量精准采集、筛选条件复杂 | 大批量采集 |

两者可以配合：**先用浏览器手动筛选拿到 URL 与 Cookie，再切 requests 挂代理批量抓**。

---

## 输出说明

文件名格式：`瓜子二手车_<城市>_<筛选标签>_<时间戳>.xlsx`（同名时自动加 `_2`、`_3` 序号，不覆盖旧结果）。

8 个字段：

| 字段 | 含义 | 示例 |
|---|---|---|
| `title` | 车辆标题 | 宝马X5 xDrive35i 尊享型 |
| `reg_year` | 上牌年份 | 2019 |
| `mileage_wan` | 行驶里程（万公里） | 15.42 |
| `city` | 车辆所在地 | 东莞 |
| `price_wan` | 售价（万元，数值型） | 18.39 |
| `price_text` | 售价原文 | 18.39万 |
| `tags` | 标签 | 已检测 / 顶配 |
| `detail_url` | 详情页链接 | https://www.guazi.com/car-detail/xxx.html |

---

## 项目结构

```
.
├── 瓜子二手车_dg.py          # 主程序：双引擎抓取 + 解析 + 落盘
├── ip_pool.py                # IP 池内核：检测 / 轮换 / 剔除 / 补货
├── proxy_api.py              # 调代理服务商 API 拉取 IP
├── requirements.txt          # Python 依赖清单
├── .gitignore                # 不上传的文件（密钥、结果、缓存）
├── api_url.example.txt       # API 链接模板（复制成 api_url.txt 再填）
├── ip_list.example.txt       # IP 名单模板
├── tools/
│   ├── diag_proxy.py         # 代理分层体检
│   ├── verify_exit_ip.py     # 验证流量是否真的走了代理
│   └── local_proxy.py        # 本地自建代理（没 IP 时验证代码链路用）
└── docs/
    └── IP池使用说明.md        # IP 池详细用法与实测数据
```

---

## IP 池是怎么工作的

```
api_url.txt ──请求──▶ 代理服务商 ──▶ 10 个 ip:port
                                      │
                                      ▼
                        写 ip_list.txt，逐个真发一次请求检测
                                      │
                      ├─ 200 且页面含关键元素 ──▶ 进池
                      └─ 超时 / 拒绝 / 被拦 / 兜底页 ──▶ 丢弃

抓取：第 1 页 → 池里第 1 个 IP
      第 2 页 → 池里第 2 个 IP     （--proxy-rotate round 顺序轮换）
      某页失败 → 换下一个 IP 重试（最多 4 次）
      同一 IP 连续失败 3 次 → 踢出池子
      池子被踢空 → 自动再调一次 API 补货
```

两个关键经验：

1. **只判「能连上」不够**。检测必须对目标站真发一次请求，并检查页面里有没有关键元素——
   否则被风控返回的兜底页也会被当成「可用」。
2. **短效代理存活期是分钟级**。实测拉取后 7~9/10 可用，8 分钟后只剩 3/10。
   所以每次运行都要重新拉，别复用上次的名单。

详见 [docs/IP池使用说明.md](docs/IP池使用说明.md)。

---

## 常见问题

<details>
<summary>弹窗报 <code>playwright: command not found</code> 或找不到浏览器</summary>

只装了 Python 包，没下载浏览器内核。执行：

```bash
python -m playwright install chromium
```
</details>

<details>
<summary>报 <code>cssselect does not seem to be installed</code></summary>

`lxml` 的 CSS 选择器需要额外依赖：

```bash
pip install cssselect
```
</details>

<details>
<summary>抓到的页面里没有车源，但状态码是 200</summary>

大概率被风控拦了，返回的是兜底页。用 `--verbose` 查看最终 URL 和 HTML 长度；
如果跳到了验证页，降低频率（拉长 `SLEEP_RANGE`）、减少页数，或改用浏览器引擎。
</details>

<details>
<summary>跑几页之后就全是 0 条</summary>

瓜子的分页上限实测是 **20 页**（第 21 页起返回空）。同时确认翻页参数用的是 `?page=N`
这种 query 形式，而不是路径段。
</details>

<details>
<summary>为什么 IP 池对浏览器引擎无效</summary>

Playwright 在启动时就把出口 IP 绑定在浏览器进程上，翻页中途换 IP 只能关掉浏览器重启，
代价远大于收益。需要 IP 池请用 `--engine requests`。
</details>

---

## 免责声明

本项目仅用于**个人学习、技术研究与交流**，不提供任何商业用途授权。

- 请遵守目标网站的 `robots.txt`、用户协议及相关法律法规；
- 请控制请求频率，避免对目标站点造成负担；
- 采集到的数据请勿用于转售、批量转载或其他侵犯他人权益的行为；
- 使用本项目产生的一切后果由使用者自行承担。

如目标网站方认为本项目存在不当之处，请联系删除。

---

## License

[MIT](LICENSE)
