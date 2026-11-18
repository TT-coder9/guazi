# -*- coding: utf-8 -*-
"""
IP 池模块（最基础版）
=====================
职责只有三件事：
  1) 存 IP（从 ip_list.txt 读，或代码里直接写死）
  2) 测 IP（对目标站点做真实请求，能用才留下）
  3) 取 IP（随机取一个给爬虫用；用坏了就踢出池子）

设计原则：不依赖第三方代理服务商的 API，先用你这 10 个静态 IP 跑通链路，
后续要换成动态拉取，只需要替换 load_ips() 的数据来源即可（见文件末尾说明）。

【单独运行自测】
    python ip_pool.py                 # 测 ip_list.txt 里的所有 IP
    python ip_pool.py 1.2.3.4:8080    # 测指定的单个 IP
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

# ==========================================================================
# 配置
# ==========================================================================
IP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ip_list.txt")

# 判定代理「能用」的检测地址。用你要爬的真实目标站点最准：
# 只要它能返回正常内容，说明这个代理对你的目标站是通的。
CHECK_URL = "https://www.guazi.com/dg/"
CHECK_KEYWORD = "car-box"        # 页面里出现这个才算真的拿到列表页，不是被拦的兜底页
CHECK_TIMEOUT = 8                # 单个代理的检测超时（秒）
CHECK_WORKERS = 10               # 并发检测线程数

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ==========================================================================
# 1) 加载 IP
# ==========================================================================
def normalize_ip(raw: str) -> Optional[str]:
    """
    把各种写法统一成 requests 需要的 'http://ip:port'。
      1.2.3.4:8080            → http://1.2.3.4:8080
      http://1.2.3.4:8080     → 原样
      user:pass@1.2.3.4:8080  → http://user:pass@1.2.3.4:8080
    没端口的直接丢弃（requests 不会自动补端口）。
    """
    s = (raw or "").strip().strip('"').strip("'")
    if not s or s.startswith("#"):
        return None
    if s.startswith(("http://", "https://", "socks5://", "socks4://")):
        return s
    # 必须是 ip:port 或 user:pass@ip:port
    if ":" not in s:
        return None
    return "http://" + s


def load_ips(path: str = IP_FILE) -> List[str]:
    """从 ip_list.txt 逐行读取，一行一个 IP，支持 # 注释和空行。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = [normalize_ip(x) for x in f.read().splitlines()]
    return [x for x in lines if x]


# ==========================================================================
# 2) 检测 IP
# ==========================================================================
def check_one(proxy: str,
              url: str = CHECK_URL,
              keyword: Optional[str] = CHECK_KEYWORD,
              timeout: float = CHECK_TIMEOUT) -> Tuple[bool, str]:
    """
    对单个代理做一次真实请求，判断是否可用。
    返回 (是否可用, 说明文字)。

    判定标准（比"能连上"严格）：
      · 拿不到 HTTP 200 → 不可用
      · 拿到了但页面里没有关键字 → 说明被反爬拦了或返回兜底页，不可用
      · 请求本身抛出异常（超时/拒绝/SSL 握手失败）→ 不可用
    """
    proxies = {"http": proxy, "https": proxy}
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout,
                         headers={"User-Agent": UA,
                                  "Accept-Language": "zh-CN,zh;q=0.9"},
                         allow_redirects=True)
    except requests.exceptions.ProxyError:
        return False, "代理连接失败"
    except requests.exceptions.ConnectTimeout:
        return False, "连接超时"
    except requests.exceptions.ReadTimeout:
        return False, "读取超时"
    except Exception as e:
        return False, f"{type(e).__name__}"

    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"

    # 被重定向到验证页也算不可用
    if "captcha" in (r.url or "").lower() or "uc.guazi.com" in (r.url or "").lower():
        return False, "被拦到验证页"

    if keyword and keyword not in r.text:
        return False, f"页面无 {keyword}（{len(r.text)}字符，疑似兜底页）"

    return True, f"OK {len(r.text)}字符"


def check_all(proxies: List[str],
              workers: int = CHECK_WORKERS) -> List[Tuple[str, bool, str]]:
    """
    并发检测一批代理，返回 [(proxy, 是否可用, 说明)]。
    并发是为了快：10 个 IP 串行要 80 秒，并发只要 8 秒。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: List[Tuple[str, bool, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(check_one, p): p for p in proxies}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                ok, msg = fut.result()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}"
            results.append((p, ok, msg))
            mark = "✓" if ok else "✗"
            print(f"  [{i}/{len(proxies)}] {mark} {p}  {msg}")

    # 按原始顺序排序，方便对照
    order = {p: i for i, p in enumerate(proxies)}
    return sorted(results, key=lambda x: order.get(x[0], 999))


# ==========================================================================
# 3) 池子：随机取 + 坏 IP 自动剔除
# ==========================================================================
class IPPool:
    """
    线程安全的 IP 池。

    用法：
        pool = IPPool.open()                 # 读 ip_list.txt → 检测 → 只留能用的
        proxies = pool.get()                 # 随机取一个，返回 {'http':..., 'https':...}
        pool.report_bad(proxies["http"])     # 某个 IP 连续失败后踢掉
    """

    def __init__(self, proxies: Optional[List[str]] = None,
                 do_check: bool = True, workers: int = CHECK_WORKERS,
                 rotate: bool = False,
                 refill: Optional[Callable[[], List[str]]] = None) -> None:
        self._lock = threading.Lock()
        self._pool: List[str] = []
        self._bad_count: Dict[str, int] = {}     # 每个 IP 的连续失败次数
        self.bad_threshold = 3                   # 连续失败 3 次才踢，避免误杀
        self.rotate = rotate                     # True=顺序轮换 / False=随机取
        self._cursor = 0                         # 轮换用的游标
        self._refill = refill                    # 池子耗尽时的重拉函数（可选）
        self._refill_times = 0                   # 重拉次数，防止无限循环

        candidates = proxies if proxies is not None else load_ips()
        if not candidates:
            raise ValueError(f"IP 池为空：检查 {IP_FILE} 是否存在且格式为 ip:port")

        if do_check:
            print(f"[IP池] 开始检测 {len(candidates)} 个代理（目标 {CHECK_URL}）…")
            t0 = time.time()
            checked = check_all(candidates, workers=workers)
            good = [p for p, ok, _ in checked if ok]
            print(f"[IP池] 检测完成，耗时 {time.time() - t0:.1f}s，"
                  f"可用 {len(good)}/{len(candidates)}")
            self._pool = good
        else:
            self._pool = list(candidates)

        if not self._pool:
            raise ValueError("所有 IP 检测均不可用，无法建立池子")

    # ---- 读 ----
    def get(self) -> Dict[str, str]:
        """
        取一个可用 IP，返回 requests 的 proxies 字典。
          rotate=True  → 顺序轮换，保证相邻两次请求不撞同一个 IP
          rotate=False → 随机取

        【池子耗尽时】若建池时传了 refill（通常是"调 API 拉新一批"），
        会自动重拉一次；重拉拿不到才抛异常，让调用方判该页失败。
        """
        if not self._pool and self._refill is not None and self._refill_times < 5:
            self._do_refill()

        with self._lock:
            if not self._pool:
                raise RuntimeError("IP 池已空，需要重新加载 IP")
            if self.rotate:
                ip = self._pool[self._cursor % len(self._pool)]
                self._cursor += 1
            else:
                ip = random.choice(self._pool)
        return {"http": ip, "https": ip}

    def _do_refill(self) -> None:
        """调用 refill 回调重拉 IP（在锁外执行，避免回调里再取锁导致死锁）。"""
        self._refill_times += 1
        print(f"[IP池] 池子已空，第 {self._refill_times} 次自动重拉 …")
        try:
            new_ips = self._refill() or []
        except Exception as e:
            print(f"[IP池] 重拉失败：{type(e).__name__}: {e}")
            return
        new_ips = [x for x in new_ips if x]
        if not new_ips:
            print("[IP池] 重拉没拿到可用 IP")
            return
        with self._lock:
            self._pool = new_ips
            self._bad_count.clear()
            self._cursor = 0
        print(f"[IP池] 重拉成功，池内 {len(new_ips)} 个")

    def all(self) -> List[str]:
        with self._lock:
            return list(self._pool)

    def size(self) -> int:
        with self._lock:
            return len(self._pool)

    # ---- 写 ----
    def report_good(self, proxy: str) -> None:
        """请求成功时调用，把连续失败计数清零。"""
        with self._lock:
            self._bad_count.pop(proxy, None)

    def report_bad(self, proxy: str) -> None:
        """请求失败时调用；连续失败达到阈值就把 IP 踢出池子。"""
        with self._lock:
            n = self._bad_count.get(proxy, 0) + 1
            self._bad_count[proxy] = n
            if n >= self.bad_threshold and proxy in self._pool:
                self._pool.remove(proxy)
                self._bad_count.pop(proxy, None)
                print(f"[IP池] 剔除失效 IP：{proxy}（剩余 {len(self._pool)} 个）")

    def refresh(self, workers: int = CHECK_WORKERS) -> int:
        """重新读 ip_list.txt 并检测一遍，返回最新可用数量。"""
        candidates = load_ips()
        checked = check_all(candidates, workers=workers)
        good = [p for p, ok, _ in checked if ok]
        with self._lock:
            self._pool = good
            self._bad_count.clear()
            self._cursor = 0
        print(f"[IP池] 刷新完成，可用 {len(good)}/{len(candidates)}")
        return len(good)

    @classmethod
    def open(cls, do_check: bool = True, workers: int = CHECK_WORKERS,
             rotate: bool = False,
             refill: Optional[Callable[[], List[str]]] = None) -> "IPPool":
        """一步到位：读文件 + 检测 + 建池。"""
        return cls(do_check=do_check, workers=workers, rotate=rotate,
                   refill=refill)


# ==========================================================================
# 自测入口
# ==========================================================================
if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 66)
    print("IP 池自测")
    print(f"检测目标：{CHECK_URL}")
    print(f"判定关键字：{CHECK_KEYWORD}")
    print("=" * 66)

    if len(sys.argv) > 1:
        targets = [normalize_ip(a) for a in sys.argv[1:]]
        targets = [t for t in targets if t]
    else:
        targets = load_ips()

    if not targets:
        print(f"\n✗ 没有可检测的 IP：请把 IP 一行一个写进 {IP_FILE}")
        raise SystemExit(1)

    print(f"\n待检测 {len(targets)} 个：")
    for t in targets:
        print(f"  · {t}")
    print()

    results = check_all(targets)
    good = [p for p, ok, _ in results if ok]

    print("\n" + "=" * 66)
    print(f"结果：{len(good)}/{len(targets)} 可用")
    if good:
        print("\n可用 IP：")
        for p in good:
            print(f"  {p}")
    else:
        print("\n✗ 全部不可用。常见原因：")
        print("   · 这些 IP 已过期 / 需账号密码认证")
        print("   · 代理不支持 https 的 CONNECT 隧道（对 guazi.com 这种 https 站点必失败）")
        print("   · 代理本身只允许特定访问白名单")
    print("=" * 66)
