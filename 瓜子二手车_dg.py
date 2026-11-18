# -*- coding: utf-8 -*-
"""
瓜子二手车（东莞站）数据采集 —— 浏览器手动筛选 + 逐页抓取 + IP 池
================================================================================
【本次改动：接入 IP 池】
  给 requests 引擎装了 IP 池：每抓一页、每重试一次都换一个 IP，
  单个 IP 连续失败 3 次自动踢出池子。相关代码集中在三处（搜 "IP池" 即可）：
    · import ip_pool                    —— 池子模块（检测 / 随机取 / 踢坏 IP）
    · fetch_html_requests(..., pool=)   —— 请求时带 proxies，失败换 IP 重试
    · main() 里的 --proxy 分支          —— 建池、打印状态
  IP 名单在 ip_list.txt 里，一行一个 ip:port，改完直接重跑即可。

【默认流程】（不带任何参数，直接运行）
  1) 启动一个「可见」的 Chromium 浏览器，停在瓜子东莞列表页；
  2) 你在浏览器里自己点 城市 / 品牌 / 价格 / 年份 等筛选条件；
  3) 回到终端按回车，程序读取当前页面 URL，作为本次抓取的起点；
  4) 浏览器【不关闭】，程序在同一个浏览器里逐页翻页抓取；
  5) 全部抓完后才关闭浏览器，并把结果保存成 Excel / CSV。

【为什么默认用可见浏览器】
  瓜子的筛选是「页面跳转 + JS 渲染」出来的，用 requests 手工拼 URL 很容易拼错
  （实测：真实筛选走路径段 /dg/bmw/price6/，并不认 ?price=5,10）。
  让用户自己在浏览器里点一遍、程序直接照抄地址栏，是最不容易出错的方式。

【安装依赖】
	 pip install requests lxml pandas openpyxl playwright
	 playwright install chromium

【运行】
	python 瓜子二手车_dg.py                    # 默认：手动筛选 + 浏览器逐页抓 3 页
	python 瓜子二手车_dg.py --pages 10          # 抓 10 页
	python 瓜子二手车_dg.py --pages 0           # 抓到没有房源为止（上限 20 页）

	# ★ IP 池（只对 requests 引擎生效）
	python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy --pages 3
	python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy --no-proxy-check
		python 瓜子二手车_dg.py --proxy --proxy-file my_ips.txt --engine requests --no-manual

	python 瓜子二手车_dg.py --engine requests --no-manual --city dg   # 不开浏览器，纯 requests
	python 瓜子二手车_dg.py --verbose                                  # 打印 HTTP 状态码等细节
	python 瓜子二手车_dg.py --extra-query "year=2018-2020"
	python 瓜子二手车_dg.py --csv                                      # 额外导出 CSV

【输出】
	瓜子二手车_东莞_<筛选标签>_<时间戳>.xlsx
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import random
import re
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

try:
	from lxml import html as LH
except ImportError:  # pragma: no cover
	raise SystemExit("缺少 lxml，请执行：pip install lxml")

# ==========================================================================
# ★ 选择器配置区 ★
# 瓜子改版时只需要改这里。F12 核实：
#   1) 打开 https://www.guazi.com/dg/ ，F12 → Elements
#   2) 点一辆车的卡片，往上找「重复出现 40 次」的容器 = CAR_CARD
#   3) Console 验证：document.querySelectorAll('.car-box .car-item').length  // 应为 40
# ==========================================================================
SEL = {
	"CAR_CARD": ".car-box .car-item",
	"TITLE": ".car-item-info-title",
	"DESC": ".car-item-info-desc",
	"PRICE_VALUE": ".car-item-info-price-value",
	"PRICE_UNIT": ".car-item-info-price-unit",
	"TAG": ".car-item-info-tag span",
	"DETAIL_LINK": "a.car-item-img",
}

# ---- 站点与分页配置 ----
SITE_HOST = "https://www.guazi.com"
CITY_PATH = "dg"                       # 东莞；佛山 fs，广州 gz，深圳 sz
DEFAULT_PAGES = 3
MAX_PAGE_LIMIT = 20                    # 实测上限：第 21 页起返回 0 条

# ---- 反爬配置 ----
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
	  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SLEEP_RANGE = (2.0, 5.0)               # 每页之间的随机延时（秒）
RETRY_PER_PAGE = 4                     # 单页最多重试次数（含首次）
RETRY_SLEEP_RANGE = (8.0, 15.0)        # 重试前的随机等待（秒）
TIMEOUT = 25

# ---- 输出 ----
# 输出文件名里的城市标签（--city 换成别的城市时，文件名也跟着变）
CITY_LABELS = {
	"dg": "东莞", "gz": "广州", "sz": "深圳", "fs": "佛山",
	"zh": "珠海", "zs": "中山", "hz": "惠州", "st": "汕头",
}


def output_basename(city: str) -> str:
	"""按 --city 生成输出文件名前缀，如 dg → 瓜子二手车_东莞。"""
	tag = CITY_LABELS.get((city or "").lower())
	return "瓜子二手车_" + (tag or (city or "未知").upper())

FIELDS = [
	"title",        # 车辆标题
	"reg_year",     # 上牌年份（2015）
	"mileage_wan",  # 行驶里程（万公里）
	"city",         # 车辆所在地
	"price_wan",    # 售价（万元）
	"price_text",   # 售价原文（8.19万）
	"tags",         # 标签（已检测/顶配…）
	"detail_url",   # 详情页链接
]

# ==========================================================================
# 输出开关：--verbose 才打印请求细节
# ==========================================================================
VERBOSE = False


def vlog(*args: Any, **kwargs: Any) -> None:
	if VERBOSE:
		print(*args, **kwargs)


# ==========================================================================
# 工具函数：文本清洗 / 数值提取
# ==========================================================================
def clean_text(v: Any) -> Optional[str]:
	"""归一化文本：连续空白（含 &nbsp; \\u00a0、全角空格 \\u3000）压成一个半角空格并去首尾。"""
	if v is None:
		return None
	s = re.sub(r"[\s\u00a0\u3000]+", " ", str(v)).strip()
	return s or None


def to_float(v: Any) -> Optional[float]:
	"""从「17.65万公里」「8.19万」「1,234元」这类文本中抽出纯数字。"""
	if v is None:
		return None
	if isinstance(v, (int, float)):
		return float(v)
	m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
	return float(m.group(0)) if m else None


def parse_desc(desc: str) -> Dict[str, Any]:
	"""解析描述行：「2015年 | 17.65万公里 | 东莞」→ {reg_year, mileage_wan, city}"""
	out: Dict[str, Any] = {"reg_year": None, "mileage_wan": None, "city": None}
	text = clean_text(desc) or ""
	parts = [p.strip() for p in re.split(r"\s*[|｜]\s*", text) if p.strip()]
	for part in parts:
		if out["reg_year"] is None:
			m = re.search(r"(19|20)\d{2}\s*年", part)
			if m:
				out["reg_year"] = int(re.search(r"(19|20)\d{2}", part).group(0))
				continue
		if out["mileage_wan"] is None:
			m = re.search(r"([\d.]+)\s*万\s*公里", part)
			if m:
				out["mileage_wan"] = float(m.group(1))
				continue
			m = re.search(r"([\d.]+)\s*公里", part)
			if m:
				out["mileage_wan"] = round(float(m.group(1)) / 10000, 4)
				continue
		if out["city"] is None and re.fullmatch(r"[\u4e00-\u9fa5]{2,8}", part):
			out["city"] = part
	return out


# --------------------------------------------------------------------------
# 瓜子的价格筛选走「路径段」，不是 query 参数
#   /dg/price6/     → 5万左右（实测 4.5~5.46 万）
#   /dg/bmw/price6/ → 宝马 + 5万左右
#   ?price=5,10 这种写法瓜子不认（实测 80 条里 47 条超出 5~10 万）
#   "左右"实测是 ±10%，抓完后可本地二次过滤
# --------------------------------------------------------------------------
PRICE_BUCKETS: Dict[int, tuple] = {
	0:  ("1万左右",   0.9,  1.1),
	1:  ("2万以下",   None, 2.0),
	2:  ("2万左右",   1.8,  2.2),
	3:  ("3万左右",   2.7,  3.3),
	4:  ("3万以下",   None, 3.0),
	5:  ("4万左右",   3.6,  4.4),
	6:  ("5万左右",   4.5,  5.5),
	7:  ("5万以下",   None, 5.0),
	8:  ("6万左右",   5.4,  6.6),
	9:  ("8万左右",   7.2,  8.8),
	10: ("10万左右",  9.0, 11.0),
	11: ("10万以下",  None, 10.0),
	12: ("15万左右", 13.5, 16.5),
	13: ("20万左右", 18.0, 22.0),
	14: ("30万左右", 27.0, 33.0),
	15: ("50万左右", 45.0, 55.0),
}


def pick_price_code(price_min: Optional[float],
					price_max: Optional[float]) -> Optional[int]:
	"""把「5~10万」映射到瓜子最接近的档位 code（用于拼 URL 路径）。"""
	lo = float(price_min) if price_min is not None else None
	hi = float(price_max) if price_max is not None else None
	if lo is None and hi is None:
		return None

	def width(code: int) -> float:
		_, b_lo, b_hi = PRICE_BUCKETS[code]
		lo_v = b_lo if b_lo is not None else 0.0
		hi_v = b_hi if b_hi is not None else 999.0
		return hi_v - lo_v

	covering: List[int] = []
	for code, (_, b_lo, b_hi) in PRICE_BUCKETS.items():
		if lo is not None:
			if b_lo is None or b_lo > lo:
				continue
		else:
			if b_lo is not None:
				continue
		if hi is not None:
			if b_hi is None or b_hi < hi:
				continue
		else:
			if b_hi is not None:
				continue
		covering.append(code)
	if covering:
		return min(covering, key=width)

	def overlap(code: int) -> float:
		_, b_lo, b_hi = PRICE_BUCKETS[code]
		a = max(lo if lo is not None else 0.0, b_lo if b_lo is not None else 0.0)
		b = min(hi if hi is not None else 999.0, b_hi if b_hi is not None else 999.0)
		return max(0.0, b - a)

	best = max(PRICE_BUCKETS, key=overlap)
	return best if overlap(best) > 0 else None


def _fmt_num(v: float) -> str:
	return str(int(v)) if float(v) == int(v) else str(v)


def describe_price_range(price_min: Optional[float],
						 price_max: Optional[float]) -> str:
	if price_min is None and price_max is None:
		return ""
	lo = _fmt_num(price_min) if price_min is not None else ""
	hi = _fmt_num(price_max) if price_max is not None else ""
	return f"价格{lo}-{hi}万"


# ==========================================================================
# ★ 文件名筛选标签：从任意形态的筛选 URL 里提取 ★
# ==========================================================================
CITY_PATHS = {
	"bj", "sh", "gz", "sz", "dg", "fs", "zh", "st", "hz", "nb", "wz", "nj",
	"su", "wx", "cz", "nt", "yz", "zj", "ta", "ly", "xc", "cd", "cq", "cs",
	"wh", "xa", "tj", "jn", "qd", "yt", "wf", "zb", "dy", "lin", "hf", "zz",
	"nc", "nn", "gl", "lz", "sy", "dl", "cc", "heb", "km", "gy", "fz", "xm",
	"qz", "sjz", "ty", "hhht", "bt", "dq", "jh", "bs", "xy", "ay", "xx",
	"shz", "kel", "wlmq", "lasa", "ls", "hk", "sanya", "san_ya",
}

_SKIP_SEGS = {"buy", "ershoufang", "sale", "list", "car", "cardetail"}

_TAG_KEYS = {
	"price": "价格",
	"auto_type": "类型",
	"year": "年份",
	"age": "车龄",
	"mileage": "里程",
	"gearbox": "变速箱",
	"fuel": "燃料",
	"color": "颜色",
}


def sanitize_tag(text: str, max_len: int = 60) -> str:
	"""把筛选条件洗成能当文件名的一段：只留中文 / 字母 / 数字 / _ / -。"""
	s = str(text or "")
	s = re.sub(r'[\\/:*?"<>|\s]+', "_", s)
	s = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]", "", s)
	s = re.sub(r"_+", "_", s).strip("_-")
	return s[:max_len]


def make_filter_tag(url: str) -> str:
	"""从筛选后的 URL 里提取品牌 / 价格档位 / query 条件，拼成文件名标签。"""
	try:
		pr = urllib.parse.urlsplit(url or "")
	except Exception:
		return ""

	parts: List[str] = []

	segs = [s for s in (pr.path or "").split("/") if s]
	for idx, seg in enumerate(segs):
		low = seg.lower()
		if idx == 0 and low in CITY_PATHS:
			continue
		if low in _SKIP_SEGS:
			continue
		m = re.fullmatch(r"price(\d+)", low)
		if m:
			code = int(m.group(1))
			parts.append(PRICE_BUCKETS.get(code, (f"price{code}",))[0])
			continue
		parts.append(seg)

	pairs = urllib.parse.parse_qsl(pr.query, keep_blank_values=True)
	unknown: List[str] = []
	for key, val in pairs:
		k = key.lower()
		if k == "page" or not val:
			continue
		label = _TAG_KEYS.get(k)
		if not label:
			unknown.append(k)
			continue
		v = val.replace(",", "-").replace(" ", "")
		if k == "price":
			v += "万"
		parts.append(f"{label}{v}")

	tag = sanitize_tag("_".join(parts))

	if unknown:
		h = hashlib.md5((url or "").encode("utf-8")).hexdigest()[:8]
		return sanitize_tag(f"{tag}_url{h}" if tag else f"url{h}")

	return tag


# ==========================================================================
# URL 工具：构造 / 改页码 / 附加 query
# ==========================================================================
def parse_extra_query(text: Optional[str]) -> List[Tuple[str, str]]:
	if not text:
		return []
	pairs: List[Tuple[str, str]] = []
	for chunk in re.split(r"[&;]", str(text)):
		chunk = chunk.strip()
		if not chunk:
			continue
		if "=" in chunk:
			k, v = chunk.split("=", 1)
		else:
			k, v = chunk, ""
		k, v = k.strip(), v.strip()
		if k:
			pairs.append((k, v))
	return pairs


def apply_extra_query(url: str,
					  pairs: Optional[List[Tuple[str, str]]]) -> str:
	if not pairs:
		return url
	try:
		pr = urllib.parse.urlsplit(url)
	except Exception:
		return url

	existing = urllib.parse.parse_qsl(pr.query, keep_blank_values=True)
	override = {k.lower() for k, _ in pairs if k.lower() != "page"}
	kept = [(k, v) for k, v in existing if k.lower() not in override]

	for k, v in pairs:
		if k.lower() == "page":
			continue
		kept.append((k, v))

	query = urllib.parse.urlencode(kept, safe=",:", quote_via=urllib.parse.quote)
	return urllib.parse.urlunsplit((pr.scheme or "https", pr.netloc,
									pr.path, query, pr.fragment))


def build_url(page_no: int, city: str = CITY_PATH,
			  brand: Optional[str] = None,
			  price_min: Optional[float] = None,
			  price_max: Optional[float] = None,
			  price_code: Optional[int] = None,
			  extra_query: Optional[List[Tuple[str, str]]] = None) -> str:
	"""【不开浏览器时】用参数拼列表页 URL（筛选走路径段）。"""
	segs: List[str] = [city]
	if brand:
		segs.append(str(brand).strip("/"))

	code = price_code
	if code is None and (price_min is not None or price_max is not None):
		code = pick_price_code(price_min, price_max)
	if code is not None:
		segs.append(f"price{int(code)}")

	path = "/" + "/".join(s for s in segs if s) + "/"

	params: List[Tuple[str, str]] = []
	if page_no and page_no > 1:
		params.append(("page", str(page_no)))
	for k, v in (extra_query or []):
		if k.lower() == "page":
			continue
		params.append((k, v))

	qs = urllib.parse.urlencode(params, safe=",", quote_via=urllib.parse.quote)
	return f"{SITE_HOST}{path}" + (f"?{qs}" if qs else "")


def set_page_param(url: str, page_no: int) -> str:
	"""把 URL 里的 page 参数改成 page_no，其余筛选条件原样保留。"""
	if page_no is None or page_no <= 0:
		raise ValueError("page_no 必须是正整数")

	pr = urllib.parse.urlsplit(url)
	pairs = urllib.parse.parse_qsl(pr.query, keep_blank_values=True)

	new_pairs: List[Tuple[str, str]] = []
	page_done = False
	for key, val in pairs:
		if key.lower() == "page":
			if not page_done:
				new_pairs.append(("page", str(page_no)))
				page_done = True
		else:
			new_pairs.append((key, val))
	if not page_done:
		new_pairs.append(("page", str(page_no)))

	query = urllib.parse.urlencode(new_pairs, safe=":,",
								   quote_via=urllib.parse.quote)
	return urllib.parse.urlunsplit((pr.scheme or "https", pr.netloc,
									pr.path, query, pr.fragment))


# ==========================================================================
# 请求头 / 拦截识别
# ==========================================================================
def build_headers(referer: Optional[str] = None) -> Dict[str, str]:
	return {
		"User-Agent": UA,
		"Referer": referer or f"{SITE_HOST}/{CITY_PATH}/",
		"Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
				   "image/avif,image/webp,*/*;q=0.8"),
		"Accept-Language": "zh-CN,zh;q=0.9",
		"Upgrade-Insecure-Requests": "1",
	}


def is_block_page(resp_text: str, final_url: str) -> Optional[str]:
	"""识别反爬拦截页。返回拦截类型，正常页面返回 None。"""
	u = (final_url or "").lower()
	if "captcha" in u or "uc.guazi.com" in u:
		return "captcha(腾讯滑块验证)"
	if "yzm" in u or "verify" in u:
		return "verify"
	head = (resp_text or "")[:20000]
	for kw in ("人机验证", "点击按钮开始验证", "访问过于频繁", "请稍后再试"):
		if kw in head:
			return f"blocked({kw})"
	return None


def looks_like_captcha_url(url: str) -> bool:
	u = (url or "").lower()
	return ("captcha" in u) or ("uc.guazi.com" in u) or ("/verify" in u)


def short_reason(reason: Optional[str]) -> str:
	r = (reason or "未知原因")
	if "captcha" in r:
		return "被拦截（captcha）"
	if "verify" in r:
		return "被拦截（verify）"
	if "HTTP" in r:
		return r
	if "内容无效" in r:
		return "内容无效（无 car-box）"
	if "异常" in r:
		return "请求异常"
	return r[:24]


# ==========================================================================
# 抓取层：Playwright 会话 + requests 会话
# ==========================================================================
_SESSION = requests.Session()


class PlaywrightSession:
	"""常驻的可见浏览器会话，供「手动筛选 → 逐页抓取」全程复用。"""

	def __init__(self, headless: bool = False,
				 user_data_dir: Optional[str] = None,
				 proxy: Optional[str] = None) -> None:
		self.headless = headless
		self.user_data_dir = user_data_dir
		self.proxy = proxy              # 当前生效的出口代理，None = 直连本机
		self._pw = None
		self._browser = None
		self._ctx = None
		self.page = None

	def __enter__(self) -> "PlaywrightSession":
		self.open()
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		self.close()

	def open(self) -> None:
		try:
			from playwright.sync_api import sync_playwright
		except ImportError:
			raise SystemExit(
				"未安装 playwright，无法使用浏览器抓取。\n"
				"  安装：pip install playwright && playwright install chromium\n"
				"  或者改用纯 requests 模式：--engine requests --no-manual"
			)

		print("正在启动浏览器…")
		self._pw = sync_playwright().start()

		# 依次尝试：自带 Chromium → 本机 Edge → 本机 Chrome。
		# 国内下载 playwright 浏览器内核常常很慢，用系统已装的浏览器照样能跑。
		attempts = [
			("playwright 自带 Chromium", {}),
			("系统 Microsoft Edge", {"channel": "msedge"}),
			("系统 Google Chrome", {"channel": "chrome"}),
		]
		last_err: Optional[Exception] = None
		for label, kwargs in attempts:
			try:
				self._browser = self._pw.chromium.launch(
					headless=self.headless, **kwargs)
				if kwargs:
					print(f"  （改用{label}启动；想用自带内核可执行 "
						  f"python -m playwright install chromium）")
				break
			except Exception as e:
				last_err = e
				vlog(f"    [playwright] {label} 启动失败：{str(e)[:100]}")

		if self._browser is None:
			try:
				self._pw.stop()
			except Exception:
				pass
			self._pw = None
			raise RuntimeError(
				"没有可用的浏览器内核，二选一：\n"
				"  ① 下载自带浏览器：python -m playwright install chromium\n"
				"  ② 装 Microsoft Edge 或 Google Chrome（程序会自动改用它们）\n"
				f"  最后一次错误：{last_err}"
			)
		self._new_context()

	def _new_context(self) -> None:
		"""
		按当前 self.proxy 建一个浏览器上下文。

		★ 代理就挂在这一层：new_context(proxy={"server": "http://ip:port"})。
		  建好之后，这个上下文里发出的所有请求 —— 页面、图片、CSS、XHR ——
		  全部从那个代理 IP 出去，而不是从你家宽带的 IP 出去。
		  实测确认：上下文级代理与 launch 级（--proxy-server）同样有效，
		  但不用重启浏览器进程，换 IP 只要零点几秒。
		"""
		kwargs: Dict[str, Any] = dict(
			user_agent=UA,
			locale="zh-CN",
			viewport={"width": 1440, "height": 900},
			extra_http_headers={"Referer": f"{SITE_HOST}/{CITY_PATH}/"},
		)
		if self.proxy:
			kwargs["proxy"] = {"server": self.proxy}
		self._ctx = self._browser.new_context(**kwargs)
		self.page = self._ctx.new_page()

	def set_proxy(self, proxy: Optional[str], keep_cookies: bool = True) -> None:
		"""
		★ 换一个出口 IP：关掉旧上下文，用新代理开一个新的。浏览器进程不重启。

		keep_cookies=True 会把旧上下文里的 Cookie 搬到新上下文 ——
		这样之前已经过掉的滑块验证、浏览痕迹不会因为换 IP 而全部丢失
		（丢 Cookie 常常意味着刚过完验证又要重新过一遍）。
		"""
		if proxy == self.proxy and self._ctx is not None:
			return                      # 还是同一个 IP，什么都不用做

		old_cookies = self.cookies() if (keep_cookies and self._ctx is not None) else []
		old_ctx, self._ctx, self.page = self._ctx, None, None
		self.proxy = proxy
		if old_ctx is not None:
			try:
				old_ctx.close()
			except Exception:
				pass
		self._new_context()
		if old_cookies:
			try:
				self._ctx.add_cookies(old_cookies)
			except Exception as e:
				vlog(f"    [playwright] Cookie 迁移失败：{type(e).__name__}")

	def close(self) -> None:
		for obj in (self._ctx, self._browser):
			if obj is None:
				continue
			try:
				obj.close()
			except Exception:
				pass
		if self._pw is not None:
			try:
				self._pw.stop()
			except Exception:
				pass
		self._pw = self._browser = self._ctx = self.page = None

	def goto(self, url: str, timeout: int = 60000) -> bool:
		"""
		打开页面。用 wait_until="commit"（只等服务器响应头回来就返回），
		不等 domcontentloaded —— 实测 Edge 无头模式下这个事件经常迟迟不触发，
		死等它会把「其实已经加载好的页面」误判成超时。内容交给 wait_ready 轮询。
		"""
		try:
			self.page.goto(url, timeout=timeout, wait_until="commit")
			return True
		except Exception as e:
			vlog(f"    [playwright] goto 异常（{type(e).__name__}），"
				 f"仍尝试取当前页面：{e}")
			return False

	def wait_ready(self, timeout: float = 25.0) -> str:
		"""轮询页面内容，直到出现房源卡片（car-box）或超时，返回那一刻的 HTML。"""
		deadline = time.time() + timeout
		html = ""
		while time.time() < deadline:
			try:
				html = self.page.content()
			except Exception:
				html = ""
			if "car-box" in html:
				break
			time.sleep(0.3)
		return html

	def wait_cards(self, timeout: int = 20000) -> int:
		try:
			self.page.wait_for_selector(SEL["CAR_CARD"], timeout=timeout)
			return self.page.locator(SEL["CAR_CARD"]).count()
		except Exception:
			return 0

	def current_url(self) -> str:
		return self.page.url

	def cookies(self) -> List[Dict[str, Any]]:
		try:
			return self._ctx.cookies() or []
		except Exception:
			return []

	def content(self) -> str:
		return self.page.content()

	def reload_current(self) -> None:
		try:
			self.page.reload(timeout=60000, wait_until="domcontentloaded")
		except Exception:
			pass


def attach_first_proxy(session: PlaywrightSession,
					   pool: Optional[Any]) -> None:
	"""给浏览器挂上池里的第一个 IP。

	手动筛选模式一打开浏览器就得走代理 —— 本机 IP 被封时，
	不带代理连筛选页都打不开，后面根本无从谈起。
	"""
	if session is None or pool is None:
		return
	try:
		ip = pool.get()["http"]
		session.set_proxy(ip)
		print(f"[IP池] 浏览器出口已切到 {ip}")
	except Exception as e:
		print(f"[警告] 给浏览器挂代理失败：{type(e).__name__}: {e}")


def fetch_html_playwright(session: PlaywrightSession, url: str,
						  page_no: int, headless: bool,
						  pool: Optional[Any] = None
						  ) -> Tuple[Optional[str], Optional[str]]:
	"""
	用浏览器抓一页。返回 (html, 失败原因)。

	【★ 代理接入点 —— 浏览器模式下 IP 池唯一生效的地方 】
	  pool 不为 None 时，每次尝试（含每次重试）都做三步：
	    1) pool.get()        从池里取一个 IP（顺序轮换，用完会自动补货）
	    2) session.set_proxy 把它挂到浏览器上下文上，从这一刻起
	                         页面 / 图片 / XHR 全部从该 IP 出去
	    3) 抓完打分          成功 report_good（失败计数清零）
	                         失败 report_bad（同一 IP 连续失败 3 次即踢出池子）

	  下面四种情况都算「这一页失败」，会换下一个 IP 重试：
	    · 代理本身连不上（页面打不开）
	    · 命中滑块验证 / 拦截页
	    · 页面打开了但没有房源卡片（多半是反爬给的兜底页）
	    · 请求超时
	"""
	last_err: Optional[str] = None

	for attempt in range(1, RETRY_PER_PAGE + 1):
		# ---- ★ 第 1 步：取 IP 并挂到浏览器上 ----
		cur_ip: Optional[str] = None
		if pool is not None:
			try:
				proxies = pool.get()
				cur_ip = proxies.get("http") or proxies.get("https")
				session.set_proxy(cur_ip)
				vlog(f"  [第 {page_no} 页] 经代理 {cur_ip} 打开"
					 f"（{attempt}/{RETRY_PER_PAGE}）：{url}")
			except Exception as e:
				print(f"  [第 {page_no} 页] ✗ 取 IP 失败：{type(e).__name__}: {e}")
				last_err = f"取 IP 失败：{e}"
				continue
		else:
			vlog(f"  [第 {page_no} 页] 浏览器打开（{attempt}/{RETRY_PER_PAGE}）：{url}")

		# ---- 第 2 步：真正打开页面 ----
		session.goto(url)
		html = session.wait_ready(timeout=25.0 if attempt == 1 else 18.0)
		n = 0
		try:
			n = session.page.locator(SEL["CAR_CARD"]).count()
		except Exception:
			pass
		final_url = session.current_url()

		vlog(f"    最终地址 {final_url}")
		vlog(f"    HTML {len(html)} 字符 | 卡片 {n} 个")

		block = is_block_page(html, final_url)
		ok = (not block) and ("car-box" in html or n > 0)

		# ---- ★ 第 3 步：给这个 IP 打分 ----
		if pool is not None and cur_ip:
			if ok:
				pool.report_good(cur_ip)
			else:
				pool.report_bad(cur_ip)
				reason = short_reason(block) if block else "页面无房源卡片"
				print(f"  [第 {page_no} 页] ✗ 代理 {cur_ip} 抓不到（{reason}），"
					  f"换下一个 IP 重试")

		if ok:
			return html, None

		last_err = block or "页面无房源卡片"

		# 没有池子时保留老逻辑：提示人工过滑块
		if pool is None and block and not headless:
			print(f"  [第 {page_no} 页] ✗ 被拦截（{short_reason(block)}）")
			print("    请在弹出的浏览器里手动把滑块验证拖过去，"
				  "看到房源列表后回到这里按回车继续本页抓取…")
			try:
				input("    >>> 过完滑块后按回车重试本页（直接回车即可）: ")
			except (EOFError, KeyboardInterrupt, OSError):
				print("    未收到确认，跳过本页")
				return None, last_err

		if attempt < RETRY_PER_PAGE:
			time.sleep(random.uniform(1.0, 3.0))

	return None, last_err or "多次重试仍失败"


def fetch_html_requests(url: str, page_no: int,
						referer: Optional[str] = None,
						pool: Optional[Any] = None,
						cookie_given: bool = False
						) -> Tuple[Optional[str], Optional[str]]:
	"""
	用 requests 抓一页 HTML。

	【IP池接入点】pool 不为 None 时，每次尝试（含重试）都会先 pool.get() 
	换一个新 IP —— 这是躲开「同一 IP 连续请求被拦」的核心动作；
	失败则 pool.report_bad()，成功则 pool.report_good() 清零计数。
	"""
	last_err: Optional[str] = None

	for attempt in range(1, RETRY_PER_PAGE + 1):
		# ---- ★ 每次尝试都换 IP ----
		proxies: Optional[Dict[str, str]] = None
		cur_ip: Optional[str] = None
		if pool is not None:
			try:
				proxies = pool.get()
				cur_ip = proxies["http"]
			except Exception as e:
				last_err = f"IP池取IP失败：{e}"
				vlog(f"    ✗ {last_err}")
				return None, last_err

		vlog(f"  [第 {page_no} 页] requests 请求（{attempt}/{RETRY_PER_PAGE}）"
			 f"{' 经代理 ' + cur_ip if cur_ip else ' 直连'}：{url}")

		try:
			resp = _SESSION.get(url, headers=build_headers(referer),
								timeout=TIMEOUT, allow_redirects=True,
								proxies=proxies)
		except requests.exceptions.RequestException as e:
			last_err = f"{type(e).__name__}: {e}"
			vlog(f"    ✗ 请求异常：{last_err}")
			if pool is not None and cur_ip:
				pool.report_bad(cur_ip)
			if attempt < RETRY_PER_PAGE:
				wait = random.uniform(*RETRY_SLEEP_RANGE)
				vlog(f"    等待 {wait:.1f} 秒后重试…")
				time.sleep(wait)
			continue

		vlog(f"    HTTP {resp.status_code} | 最终地址 {resp.url}")
		vlog(f"    HTML {len(resp.text)} 字符")

		if resp.status_code != 200:
			last_err = f"HTTP {resp.status_code}"
			if pool is not None and cur_ip:
				pool.report_bad(cur_ip)
			if attempt < RETRY_PER_PAGE:
				wait = random.uniform(*RETRY_SLEEP_RANGE)
				vlog(f"    等待 {wait:.1f} 秒后重试…")
				time.sleep(wait)
			continue

		block = is_block_page(resp.text, resp.url)
		if block:
			last_err = block
			vlog(f"    ✗ 被反爬拦截：{block}")
			if pool is not None and cur_ip:
				pool.report_bad(cur_ip)
			if attempt < RETRY_PER_PAGE:
				wait = random.uniform(*RETRY_SLEEP_RANGE)
				vlog(f"    等待 {wait:.1f} 秒后重试…")
				time.sleep(wait)
			continue

		# HTTP 200 不代表内容有效：实测偶发返回约 12 万字符、不含 car-box 的兜底页
		if "car-box" not in resp.text:
			last_err = f"内容无效（HTML {len(resp.text)} 字符，无 car-box）"
			vlog(f"    ✗ {last_err}")
			if pool is not None and cur_ip:
				pool.report_bad(cur_ip)
			if attempt < RETRY_PER_PAGE:
				wait = random.uniform(*RETRY_SLEEP_RANGE)
				vlog(f"    等待 {wait:.1f} 秒后重试…")
				time.sleep(wait)
			continue

		# ---- ★ 这个 IP 成功过一页，清零它的失败计数 ----
		if pool is not None and cur_ip:
			pool.report_good(cur_ip)
			vlog(f"    ✓ 本页由 {cur_ip} 抓取成功")

		return resp.text, None

	return None, last_err


def apply_browser_cookies(cookies: List[Dict[str, Any]]) -> int:
	"""把浏览器里拿到的 cookie 灌进 requests 的 Session。"""
	n = 0
	for c in cookies or []:
		try:
			name, value = c.get("name"), c.get("value")
			if not name:
				continue
			_SESSION.cookies.set(name, value,
								 domain=c.get("domain") or ".guazi.com",
								 path=c.get("path") or "/")
			n += 1
		except Exception:
			continue
	return n


# ==========================================================================
# 解析层
# ==========================================================================
def parse_page(html_text: str, page_no: int) -> List[Dict[str, Any]]:
	"""解析一页 HTML，返回房源列表。"""
	if not html_text:
		return []

	try:
		doc = LH.fromstring(html_text)
	except Exception as e:
		print(f"    ✗ HTML 解析失败：{type(e).__name__}: {e}")
		return []

	cards = doc.cssselect(SEL["CAR_CARD"])
	if not cards:
		n_li = len(re.findall(r"<li[\s>]", html_text))
		has_box = "car-box" in html_text
		print(f"    ✗ 未找到车辆卡片（选择器 {SEL['CAR_CARD']}）")
		print(f"      [诊断] HTML {len(html_text)} 字符 | 含 car-box: {has_box} | <li> 数 {n_li}")
		print("      → 若含 car-box 但抽不到，多半是瓜子改版，请用 F12 更新 SEL")
		return []

	rows: List[Dict[str, Any]] = []
	for card in cards:
		try:
			title_el = card.cssselect(SEL["TITLE"])
			title = clean_text(title_el[0].text_content()) if title_el else None

			desc_el = card.cssselect(SEL["DESC"])
			desc = clean_text(desc_el[0].text_content()) if desc_el else ""
			desc_info = parse_desc(desc)

			pv_el = card.cssselect(SEL["PRICE_VALUE"])
			pu_el = card.cssselect(SEL["PRICE_UNIT"])
			if pv_el:
				price_num = clean_text(pv_el[0].text_content())
				price_unit = clean_text(pu_el[0].text_content()) if pu_el else "万"
				price_text = f"{price_num}{price_unit or ''}"
			else:
				price_num, price_text = None, None

			tags = [clean_text(t.text_content()) for t in card.cssselect(SEL["TAG"])]
			tags = [t for t in tags if t]

			link_el = card.cssselect(SEL["DETAIL_LINK"])
			href = link_el[0].get("href") if link_el else None
			if href and href.startswith("/"):
				href = SITE_HOST + href

			rows.append({
				"title": title,
				"reg_year": desc_info["reg_year"],
				"mileage_wan": desc_info["mileage_wan"],
				"city": desc_info["city"],
				"price_wan": to_float(price_num) if price_num else None,
				"price_text": price_text,
				"tags": "/".join(tags) if tags else None,
				"detail_url": href,
			})
		except Exception as e:
			vlog(f"    [警告] 有一条房源解析失败：{type(e).__name__}: {e}")
			continue

	return rows


# ==========================================================================
# 清洗层
# ==========================================================================
def clean_rows(rows: List[Dict[str, Any]]) -> pd.DataFrame:
	"""按 FIELDS 定列 → 按 detail_url 去重 → 数值收敛 → 丢空行。"""
	if not rows:
		return pd.DataFrame(columns=FIELDS)

	df = pd.DataFrame(rows)
	for col in FIELDS:
		if col not in df.columns:
			df[col] = None
	df = df[FIELDS].copy()

	before = len(df)
	df = df.drop_duplicates(subset=["detail_url"], keep="first")
	if len(df) != before:
		print(f"[清洗] 去重 {before} → {len(df)} 条")

	df = df[(df["title"].notna()) | (df["detail_url"].notna())]

	df["reg_year"] = pd.to_numeric(df["reg_year"], errors="coerce").astype("Int64")
	for c in ("mileage_wan", "price_wan"):
		df[c] = pd.to_numeric(df[c], errors="coerce").round(4)

	return df.reset_index(drop=True)


# ==========================================================================
# 落盘层
# ==========================================================================
def save_dataframe(df: pd.DataFrame, also_csv: bool = False,
				   tag: str = "", city: str = CITY_PATH) -> None:
	if df.empty:
		print("\n没有采集到任何数据，不生成文件。")
		return

	base_dir = os.path.dirname(os.path.abspath(__file__))
	stamp = time.strftime("%Y%m%d_%H%M")
	base_stem = output_basename(city) + (f"_{tag}" if tag else "") + f"_{stamp}"

	# 同一分钟内重复运行（比如先直连试一次、再挂 IP 池试一次）会撞名，
	# 这里自动加 _2 / _3 序号，绝不覆盖上一次的结果文件。
	stem = base_stem
	seq = 2
	while (os.path.exists(os.path.join(base_dir, stem + ".xlsx"))
		   or (also_csv and os.path.exists(os.path.join(base_dir, stem + ".csv")))):
		stem = f"{base_stem}_{seq}"
		seq += 1

	xlsx_path = os.path.join(base_dir, stem + ".xlsx")
	try:
		with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
			df.to_excel(writer, index=False, sheet_name="瓜子二手车")
			ws = writer.sheets["瓜子二手车"]
			widths = {"title": 46, "detail_url": 58, "tags": 18,
					  "price_text": 12, "city": 8}
			for idx, col in enumerate(df.columns, start=1):
				letter = ws.cell(row=1, column=idx).column_letter
				ws.column_dimensions[letter].width = widths.get(col, 12)
			ws.freeze_panes = "A2"
		print(f"\n✓ Excel 已保存：{xlsx_path}")
	except Exception as e:
		print(f"\n✗ Excel 保存失败（{type(e).__name__}: {e}），可只用 --csv 导出")

	if also_csv:
		csv_path = os.path.join(base_dir, stem + ".csv")
		try:
			df.to_csv(csv_path, index=False, encoding="utf-8-sig",
					  quoting=csv.QUOTE_MINIMAL)
			print(f"✓ CSV  已保存：{csv_path}")
		except Exception as e:
			print(f"✗ CSV 保存失败：{type(e).__name__}: {e}")


def print_summary(df: pd.DataFrame) -> None:
	if df.empty:
		return
	print("\n" + "=" * 62)
	print(f"共采集 {len(df)} 条车源")
	if df["price_wan"].notna().any():
		print(f"  价格区间：{df['price_wan'].min():.2f} ~ {df['price_wan'].max():.2f} 万 "
			  f"（均价 {df['price_wan'].mean():.2f} 万）")
	else:
		print("  价格：无有效数据")
	if df["reg_year"].notna().any():
		print(f"  年份区间：{int(df['reg_year'].min())} ~ {int(df['reg_year'].max())}")
	if df["mileage_wan"].notna().any():
		print(f"  里程区间：{df['mileage_wan'].min():.2f} ~ {df['mileage_wan'].max():.2f} 万公里")
	print(f"  城市分布：{df['city'].value_counts(dropna=True).head(5).to_dict()}")
	print("=" * 62)
	print("\n--- 前 5 行预览 ---")
	preview_cols = ["title", "reg_year", "mileage_wan", "price_text", "detail_url"]
	print(df[preview_cols].head(5).to_string(index=False))


# ==========================================================================
# 手动筛选
# ==========================================================================
MANUAL_PROMPT = """
==============================================================
 浏览器已打开，请在里面点选 城市 / 品牌 / 价格 / 年份 等筛选条件。
 选好后回到这里按回车，程序将从【当前页面 URL】开始逐页抓取。
--------------------------------------------------------------
  · 什么都不点直接回车 = 抓该城市全量
  · 看到滑块验证，先在浏览器里拖过去再按回车
=============================================================="""


def manual_pick_url(session: PlaywrightSession,
					start_url: str) -> Optional[str]:
	"""在已打开的浏览器里让用户手动筛选，按回车后返回筛选后的 URL。"""
	session.goto(start_url)
	n = session.wait_cards(timeout=25000)
	if n:
		print(f"✓ 已加载房源列表，当前页 {n} 条")
	else:
		print("⚠ 没等到房源列表（可能要过滑块验证）。"
			  "你可以在浏览器里处理，处理完再回终端按回车。")

	print(MANUAL_PROMPT)
	try:
		input(">>> 筛选完成后，按回车开始抓取（Ctrl+C 放弃）: ")
	except (EOFError, KeyboardInterrupt, OSError):
		print("\n[手动筛选] 未收到回车信号，改为用命令行参数抓取。")
		return None

	try:
		return session.current_url()
	except Exception as e:
		print(f"  ⚠ 读取页面 URL 失败（浏览器可能已被关闭）：{type(e).__name__}")
		return None


# ==========================================================================
# ★ IP 池构建
# ==========================================================================
def build_ip_pool(proxy_file: Optional[str], do_check: bool, workers: int,
				  rotate: bool = False, use_api: bool = False):
	"""
	建 IP 池。失败（文件不存在 / 全部不可用）时返回 None，让调用方决定降级还是退出。
	这一步会先对目标站点真发一次请求，所以能看到「几个 IP 真能用」。

	use_api=True 时先调服务商 API 拉一批新 IP（并落盘到名单文件）再检测 —— 
	短效代理存活只有几分钟，拿旧名单抓必然大量失败，所以每次抓取前都要重拉。
	"""
	try:
		import ip_pool as IP
	except ImportError:
		print("✗ 找不到 ip_pool.py，请确认它和本脚本在同一目录")
		return None

	# ---- ★ 走 API 拉一批新 IP ----
	if use_api:
		try:
			import proxy_api as PA
		except ImportError:
			print("✗ 找不到 proxy_api.py，API 拉取不可用")
			return None
		try:
			text = PA.fetch(PA.read_api_url())
			raw = PA.parse(text)
		except SystemExit as e:
			print(f"✗ {e}")
			return None

		if not raw:
			print("✗ API 没返回任何 IP：可能订单过期、提取次数用完或未绑定白名单")
			return None

		ips = [x for x in (IP.normalize_ip(r) for r in raw) if x]
		print(f"[IP池] API 拉到 {len(ips)} 个 IP，同步覆盖写入 {IP.IP_FILE}")
		try:
			PA.write_ips(raw, IP.IP_FILE)
		except Exception as e:
			print(f"  （名单文件写入失败：{type(e).__name__}，不影响本次建池）")

		def _refill_from_api() -> List[str]:
			"""池子被踢空后自动再拉一批（长页数抓取时 IP 会中途死光）。"""
			text2 = PA.fetch(PA.read_api_url())
			return [x for x in (IP.normalize_ip(r) for r in PA.parse(text2)) if x]

		try:
			return IP.IPPool(ips, do_check=do_check, workers=workers,
							 rotate=rotate, refill=_refill_from_api)
		except ValueError as e:
			print(f"✗ 建池失败：{e}")
			return None

	if proxy_file:
		ips = IP.load_ips(proxy_file)
		if not ips:
			print(f"✗ 代理文件里没读到有效 IP：{proxy_file}（格式应是一行一个 ip:port）")
			return None
		try:
			return IP.IPPool(ips, do_check=do_check, workers=workers, rotate=rotate)
		except ValueError as e:
			print(f"✗ 建池失败：{e}")
			return None

	try:
		return IP.IPPool.open(do_check=do_check, workers=workers, rotate=rotate)
	except ValueError as e:
		print(f"✗ 建池失败：{e}")
		return None


# ==========================================================================
# 主流程
# ==========================================================================
def crawl(pages: int,
		  engine: str,
		  headless: bool,
		  also_csv: bool,
		  city: str = CITY_PATH,
		  start_url: Optional[str] = None,
		  filter_tag: str = "",
		  brand: Optional[str] = None,
		  price_min: Optional[float] = None,
		  price_max: Optional[float] = None,
		  price_code: Optional[int] = None,
		  extra_query: Optional[List[Tuple[str, str]]] = None,
		  session: Optional[PlaywrightSession] = None,
		  auto_fallback: bool = True,
		  pool: Optional[Any] = None) -> pd.DataFrame:
	"""主采集流程：逐页抓取 → 解析 → 汇总 → 清洗 → 保存。"""
	unlimited = pages is None or pages <= 0
	if unlimited:
		pages = MAX_PAGE_LIMIT
		print(f"[提示] --pages 0 → 抓到没有房源为止（最多 {MAX_PAGE_LIMIT} 页）")
	if pages > MAX_PAGE_LIMIT:
		print(f"[提示] 分页上限是 {MAX_PAGE_LIMIT} 页，已将 --pages 从 {pages} 收敛")
		pages = MAX_PAGE_LIMIT
	if pages < 1:
		raise SystemExit("--pages 至少为 1（或传 0 表示抓到没有为止）")

	def url_for(p: int) -> str:
		if start_url:
			return set_page_param(start_url, p)
		return build_url(p, city, brand, price_min, price_max,
						 price_code, extra_query)

	print("=" * 62)
	print(f"目标：瓜子二手车（{city}站）")
	print(f"引擎：{engine} | 页数：{pages} | 上限：{MAX_PAGE_LIMIT}")
	print(f"IP 池：{'启用，可用 ' + str(pool.size()) + ' 个' if pool else '未启用（直连）'}")
	if start_url:
		print(f"起点 URL（筛选后）：{start_url}")
		print(f"筛选标签：{filter_tag or '（无）'}")
	else:
		code = price_code
		if code is None and (price_min is not None or price_max is not None):
			code = pick_price_code(price_min, price_max)
		bucket = PRICE_BUCKETS.get(code, (None,))[0] if code is not None else None
		print(f"筛选：brand={brand or '不限'} | "
			  f"价格={describe_price_range(price_min, price_max) or '不限'} "
			  f"→ 瓜子档位={bucket or '无（抓全量后本地过滤）'}")
		if extra_query:
			print(f"附加 query：{extra_query}")
	print(f"第 1 页地址：{url_for(1)}")
	print("=" * 62)

	owns_session = session is None and engine == "playwright"
	if owns_session:
		session = PlaywrightSession(headless=headless)
		try:
			session.open()
		except SystemExit:
			raise
		except Exception as e:
			print(f"[警告] 浏览器启动失败（{type(e).__name__}: {e}），改用 requests")
			engine = "requests"
			session = None
			owns_session = False

	all_rows: List[Dict[str, Any]] = []

	try:
		for p in range(1, pages + 1):
			url = url_for(p)
			referer = (start_url if (start_url and p == 1)
					   else (url_for(p - 1) if p > 1 else None))

			print(f"[第 {p}/{pages} 页] 抓取中… ", end="", flush=True)

			html_text: Optional[str] = None
			reason: Optional[str] = None

			if engine == "playwright" and session is not None:
				html_text, reason = fetch_html_playwright(
					session, url, p, headless=headless, pool=pool)
			else:
				html_text, reason = fetch_html_requests(url, p, referer, pool=pool)
				if html_text is None and auto_fallback:
					fb_headless = headless
					if not fb_headless and not sys.stdin.isatty():
						fb_headless = True
						vlog("      （当前非交互终端，兜底时强制无头）")
					print("(重试中)", end="", flush=True)
					try:
						with PlaywrightSession(headless=fb_headless) as fb:
							html_text, reason = fetch_html_playwright(
								fb, url, p, headless=fb_headless, pool=pool)
					except SystemExit:
						pass
					except Exception as e:
						vlog(f"    [兜底] 浏览器重试失败：{type(e).__name__}: {e}")

			if not html_text:
				print(f"✗ {short_reason(reason)}，已跳过")
				if unlimited:
					print(f"    第 {p} 页没拿到内容，停止翻页")
					break
				continue

			page_rows = parse_page(html_text, p)
			all_rows.extend(page_rows)

			if not page_rows:
				print("✗ 0 条（见上方诊断）")
				if unlimited:
					print(f"    第 {p} 页没有房源了，停止翻页")
					break
			else:
				print(f"✓ {len(page_rows)} 条（累计 {len(all_rows)} 条）")

			if p < pages:
				wait = random.uniform(*SLEEP_RANGE)
				print(f"    等待 {wait:.1f} 秒后翻下一页…")
				time.sleep(wait)
	finally:
		if owns_session and session is not None:
			session.close()

	print(f"\n原始累计 {len(all_rows)} 条，开始清洗…")
	df = clean_rows(all_rows)

	if (price_min is not None or price_max is not None) and not df.empty:
		before = len(df)
		mask = df["price_wan"].notna()
		if price_min is not None:
			mask &= (df["price_wan"] >= float(price_min))
		if price_max is not None:
			mask &= (df["price_wan"] <= float(price_max))
		dropped = before - int(mask.sum())
		if dropped:
			print(f"[过滤] 按 {describe_price_range(price_min, price_max)} 剔除 {dropped} 条"
				  f"（{before} → {before - dropped}）")
		df = df[mask].reset_index(drop=True)
		if df.empty:
			print("[警告] 本地过滤后一条都不剩。瓜子价格档位比你给的区间粗，"
				  "建议放宽区间或改用 --price-code")

	if not filter_tag and (brand or price_min is not None or price_max is not None):
		tag_parts = [str(brand)] if brand else []
		pr = describe_price_range(price_min, price_max)
		if pr:
			tag_parts.append(pr)
		filter_tag = sanitize_tag("_".join(tag_parts))

	print_summary(df)
	save_dataframe(df, also_csv=also_csv, tag=filter_tag, city=city)
	return df


# ==========================================================================
# 命令行 / 入口
# ==========================================================================
def _url_signature(url: str) -> Tuple[str, str, str]:
	try:
		pr = urllib.parse.urlsplit(url or "")
		pairs = [(k, v) for k, v in
				 urllib.parse.parse_qsl(pr.query, keep_blank_values=True)
				 if k.lower() != "page"]
		return (pr.netloc.lower(), pr.path.rstrip("/").lower(),
				urllib.parse.urlencode(sorted(pairs)))
	except Exception:
		return ("", "", "")


def warn_empty_tag(url: str, base_url: str) -> None:
	if _url_signature(url) == _url_signature(base_url):
		return
	try:
		pr = urllib.parse.urlsplit(url or "")
		path, query = pr.path, pr.query
	except Exception:
		path, query = "", ""
	print("⚠ 无法从 URL 解析出筛选标签，文件名将不含筛选信息。")
	print(f"  URL  : {url}")
	print(f"  path : {path or '（空）'}")
	print(f"  query: {query or '（空）'}")
	print("  → 如果这是新的筛选形态，把它发给维护者以便补充 make_filter_tag() 的规则")


def parse_args() -> argparse.Namespace:
	ap = argparse.ArgumentParser(
		description="瓜子二手车采集器 —— 浏览器手动筛选 + 逐页抓取 + IP 池",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""常用用法：
   1) 默认（推荐）
        python 瓜子二手车_dg.py
        弹可见浏览器 → 你点筛选 → 回车 → 同一个浏览器逐页翻 → 保存

	 2) ★ 用 IP 池抓（本次新增，只对 requests 引擎生效）
				python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy --pages 3

	 3) 不开浏览器，纯 requests 抓全量（不加 --proxy 就是直连）
				python 瓜子二手车_dg.py --engine requests --no-manual --city dg

	 4) 不开筛选界面，但用浏览器翻页（最不容易被拦，但不走 IP 池）
				python 瓜子二手车_dg.py --engine playwright --no-manual --city dg

	 5) 先单独体检 IP 池，不抓数据
				python ip_pool.py
""",
	)

	ap.add_argument("--pages", type=int, default=DEFAULT_PAGES,
					help=f"抓取页数，默认 {DEFAULT_PAGES}；传 0 = 抓到没有房源为止"
						 f"（上限 {MAX_PAGE_LIMIT}）")

	ap.add_argument("--engine", choices=["playwright", "requests"], default="playwright",
					help="抓取引擎，默认 playwright（可见浏览器逐页翻）；"
						 "requests 不开浏览器，速度快，且【只有它走 IP 池】")

	ap.add_argument("--headless", action="store_true",
					help="无头模式。仅 --engine playwright 且 --no-manual 时生效")

	ap.add_argument("--manual", dest="manual", action="store_true",
					help="在浏览器里手动点筛选再抓取（默认行为）")
	ap.add_argument("--no-manual", dest="manual", action="store_false",
					help="跳过筛选界面，直接用命令行参数抓取")
	ap.set_defaults(manual=True)

	ap.add_argument("--city", default=CITY_PATH,
					help=f"城市拼音路径，默认 {CITY_PATH}（东莞）。如 gz / sz / fs")

	ap.add_argument("--brand", default=None, help="品牌路径段，如 bmw。仅 --no-manual 时生效")

	ap.add_argument("--price-min", dest="price_min", type=float, default=None,
					help="最低价（万）。仅 --no-manual 时生效，会先映射成最接近的档位，抓完再本地精确过滤")
	ap.add_argument("--price-max", dest="price_max", type=float, default=None,
					help="最高价（万）。仅 --no-manual 时生效")

	ap.add_argument("--price-code", dest="price_code", type=int, default=None,
					choices=list(range(0, 16)),
					help="直接用瓜子价格档位 0~15（拼成 /dg/priceN/）。"
						 "0=1万左右 1=2万以下 2=2万左右 3=3万左右 4=3万以下 "
						 "5=4万左右 6=5万左右 7=5万以下 8=6万左右 9=8万左右 "
						 "10=10万左右 11=10万以下 12=15万左右 13=20万左右 "
						 "14=30万左右 15=50万左右")

	ap.add_argument("--extra-query", dest="extra_query", default=None,
					help='通用 query 透传，如 "year=2018-2020&fuel=汽油"')

	ap.add_argument("--csv", action="store_true", help="额外导出 CSV")

	ap.add_argument("--no-fallback", action="store_true",
					help="关闭「requests 彻底失败时临时开浏览器重试本页」")

	# ---- ★ IP 池参数 ----
	ap.add_argument("--proxy", action="store_true",
					help="启用 IP 池（读 ip_list.txt，先检测再使用）。"
						 "仅 --engine requests 生效")
	ap.add_argument("--proxy-file", dest="proxy_file", default=None,
					help="代理名单文件，默认 ip_list.txt，一行一个 ip:port")
	ap.add_argument("--no-proxy-check", dest="proxy_check", action="store_false",
					help="跳过建池前的可用性检测，全部 IP 直接进池（快，但会带着坏 IP 跑）")
	ap.set_defaults(proxy_check=True)
	ap.add_argument("--proxy-workers", dest="proxy_workers", type=int, default=10,
					help="并发检测线程数，默认 10")

	ap.add_argument("--proxy-rotate", dest="proxy_rotate",
					choices=["random", "round"], default="round",
					help="取 IP 策略：round=顺序轮换（默认，保证相邻请求不撞同一 IP）；"
						 "random=随机取")

	ap.add_argument("--proxy-api", dest="proxy_api", action="store_true",
					help="建池前先调服务商 API 拉一批新 IP（链接写在 api_url.txt）。"
						 "短效代理只有几分钟生命周期，正式抓取建议都带上这个参数")

	ap.add_argument("--verbose", action="store_true",
					help="打印请求细节（HTTP 状态码、HTML 长度、重试过程、当前用的哪个 IP）")

	return ap.parse_args()


def main() -> None:
	global VERBOSE

	args = parse_args()

	if sys.platform.startswith("win"):
		try:
			sys.stdout.reconfigure(encoding="utf-8", errors="replace")
			sys.stderr.reconfigure(encoding="utf-8", errors="replace")
		except Exception:
			pass

	VERBOSE = args.verbose

	extra_query = parse_extra_query(args.extra_query)
	base_url = f"{SITE_HOST}/{args.city}/"

	# ------------------------------------------------------------------
	# 阶段 0：IP 池（--proxy 才启用）
	# ------------------------------------------------------------------
	pool = None
	if args.proxy or args.proxy_api:
		# 两套引擎都支持 IP 池：requests 用 proxies 参数，浏览器用上下文级代理
		pool = build_ip_pool(args.proxy_file, args.proxy_check,
							 args.proxy_workers,
							 rotate=(args.proxy_rotate == "round"),
							 use_api=args.proxy_api)
		if pool is None:
			print("[警告] IP 池构建失败，本次改为直连抓取\n")
		else:
			target = "浏览器" if args.engine == "playwright" else "requests"
			print(f"[IP池] 就绪，可用 {pool.size()} 个（引擎：{target}，"
				  f"取 IP 策略：{args.proxy_rotate}）：{pool.all()}\n")

	start_url: Optional[str] = None
	filter_tag = ""
	session: Optional[PlaywrightSession] = None
	manual = args.manual

	# ------------------------------------------------------------------
	# 阶段 1：手动筛选（默认）
	# ------------------------------------------------------------------
	if manual:
		print("=" * 62)
		print("手动筛选模式：即将打开可见浏览器，请你自己点选筛选条件")
		print("（不想开浏览器就加 --no-manual，改用 --brand / --price-min 等参数）")
		print("=" * 62)

		session = PlaywrightSession(headless=False)
		try:
			session.open()
		except SystemExit:
			raise
		except Exception as e:
			print(f"⚠ 浏览器启动失败（{type(e).__name__}: {e}），"
				  "改为不开浏览器，直接用命令行参数抓取")
			manual = False
			session = None

		if session is not None:
			attach_first_proxy(session, pool)
			start_url = manual_pick_url(session, base_url)

			if start_url and looks_like_captcha_url(start_url):
				print(f"\n⚠ 你按回车时浏览器停在验证页上：{start_url}")
				print("  这不是房源列表，抓下去必然是 0 条。")
				print("  已回退到命令行参数模式（请重新运行并先过掉滑块）。")
				start_url = None

			if start_url:
				n = apply_browser_cookies(session.cookies())
				if n:
					vlog(f"✓ 已把浏览器里的 {n} 条 cookie 灌进 requests 会话")

				filter_tag = make_filter_tag(start_url)
				print("=" * 62)
				print(f"已捕获筛选 URL: {start_url}")
				print(f"解析出的筛选标签：{filter_tag or '（无）'}")
				print("=" * 62)
				if not filter_tag:
					warn_empty_tag(start_url, base_url)
			else:
				print("\n[回退] 没有捕获到筛选 URL，改用命令行参数模式抓取")

	# ------------------------------------------------------------------
	# 阶段 2：抓取
	# ------------------------------------------------------------------
	engine = args.engine or "playwright"

	headless = False if manual else bool(args.headless)
	if args.headless and manual:
		print("[提示] 手动筛选模式必须有可见浏览器，--headless 已忽略")

	try:
		crawl(pages=args.pages,
			  engine=engine,
			  headless=headless,
			  also_csv=args.csv,
			  city=args.city,
			  start_url=start_url,
			  filter_tag=filter_tag,
			  brand=None if manual else args.brand,
			  price_min=None if manual else args.price_min,
			  price_max=None if manual else args.price_max,
			  price_code=None if manual else args.price_code,
			  extra_query=extra_query,
			  session=session if engine == "playwright" else None,
			  auto_fallback=not args.no_fallback,
			  pool=pool)
	except KeyboardInterrupt:
		print("\n\n已手动中断（Ctrl+C）")
	except SystemExit:
		raise
	except Exception as e:
		print(f"\n[致命错误] {type(e).__name__}: {e}")
		import traceback
		traceback.print_exc()
	finally:
		if session is not None:
			session.close()
			print("浏览器已关闭")


if __name__ == "__main__":
	main()
