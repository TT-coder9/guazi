# -*- coding: utf-8 -*-
"""
验证浏览器出口 IP 是否真的等于代理 IP（两种挂法对照）。

launch 级代理 vs 上下文级代理 —— 哪种在 Chromium 里真的生效？

用同一批「刚刚拉到手」的新鲜 IP，分别用两种方式挂上去，看出口 IP 有没有变。
要先排除「IP 已经过期」这个干扰因素，所以每次都现拉现测。
"""
import os
import re
import sys
import time

# 让 tools/ 下的脚本能 import 根目录的 ip_pool / proxy_api
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ip_pool as IP
import proxy_api as PA

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
IP_ECHO = "http://myip.ipip.net"
CTX_ARGS = dict(user_agent=UA, locale="zh-CN",
                viewport={"width": 1440, "height": 900})


def read_ip(page, timeout=20):
    t0 = time.time()
    err = ""
    try:
        page.goto(IP_ECHO, timeout=timeout * 1000, wait_until="commit")
    except Exception as e:
        err = type(e).__name__
    html = ""
    while time.time() - t0 < timeout:
        try:
            html = page.content()
        except Exception:
            html = ""
        if "IP" in html and re.search(r"([0-9]{1,3}\.){3}[0-9]{1,3}", html):
            break
        time.sleep(0.3)
    m = re.search(r"当前\s*IP[:：]\s*([0-9.]+)", html) \
        or re.search(r"([0-9]{1,3}\.){3}[0-9]{1,3}", html)
    if not m:
        return "", err
    return (m.group(1) if m.lastindex else m.group(0)), err


def main():
    raw = PA.parse(PA.fetch(PA.read_api_url()))
    ips = [x for x in (IP.normalize_ip(r) for r in raw) if x][:3]
    print("现拉到的 IP：", ips, "\n")

    with sync_playwright() as pw:
        # ---- 基线 ----
        b = pw.chromium.launch(headless=True, channel="msedge")
        pg = b.new_context(**CTX_ARGS).new_page()
        print("[基线] 直连 →", read_ip(pg))
        b.close()

        for p in ips:
            # ---- A：launch 级（--proxy-server）----
            try:
                b = pw.chromium.launch(headless=True, channel="msedge",
                                       proxy={"server": p})
                pg = b.new_context(**CTX_ARGS).new_page()
                res = read_ip(pg)
                b.close()
            except Exception as e:
                res = (f"launch 失败 {type(e).__name__}", "")
            print(f"[launch 级] {p}\n    出口 → {res[0]}  {res[1]}")

            # ---- B：上下文级 ----
            b = pw.chromium.launch(headless=True, channel="msedge")
            try:
                ctx = b.new_context(proxy={"server": p}, **CTX_ARGS)
                pg = ctx.new_page()
                res2 = read_ip(pg)
                ctx.close()
            except Exception as e:
                res2 = (f"context 失败 {type(e).__name__}", "")
            b.close()
            print(f"[上下文级] {p}\n    出口 → {res2[0]}  {res2[1]}\n")

        print("=" * 70)
        print("判定：出口 IP 等于代理 IP 或与本机(120.235.59.58)不同 → 该方式生效")
        print("=" * 70)


if __name__ == "__main__":
    main()
