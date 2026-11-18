# -*- coding: utf-8 -*-
"""验证：抓取流量确实走了代理（对比本机出口 IP 与代理出口 IP）。

用法：python tools/verify_exit_ip.py
"""
import os
import sys

import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 本文件在 tools/ 下，把项目根目录加入模块搜索路径，才能 import ip_pool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ip_pool as IP

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"
IP_ECHO = "http://myip.ipip.net"


def my_ip(proxies=None):
    try:
        r = requests.get(IP_ECHO, proxies=proxies, timeout=8,
                         headers={"User-Agent": UA})
        return r.text.strip().replace("\n", " ")
    except Exception as e:
        return f"失败 {type(e).__name__}"


print("=" * 66)
print("① 本机直连出口 IP")
print("   " + my_ip())

print("\n② 经 IP 池里的每个代理，各自的出口 IP")
ok = 0
for p in IP.load_ips()[:10]:
    line = my_ip({"http": p, "https": p})
    if "失败" not in line:
        ok += 1
    print(f"   {p:34s} → {line}")

print("\n" + "=" * 66)
print(f"有 {ok} 个代理能正常回显出口 IP；若与本机 IP 不同，说明流量确实经代理转发")
print("=" * 66)
