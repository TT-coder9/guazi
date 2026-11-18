# -*- coding: utf-8 -*-
"""
代理体检（串行版）
==================
并发检测会把同一批代理瞬时打满，遇到限频的代理就全部误判为"不可用"。
这个脚本串行逐个测，并且分两层判定，用来区分到底是
  A) TCP 都连不上（代理已死 / 需要白名单）
  B) TCP 能连但转发不了 https（代理不支持 CONNECT 隧道）
  C) 能转发但目标站被反爬拦了

用法：
    python diag_proxy.py
"""

import os
import socket
import sys
import time
from typing import List, Optional

import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 项目根目录（本文件在 tools/ 下，所以往上退一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IP_FILE = os.path.join(PROJECT_ROOT, "ip_list.txt")


def load_targets() -> List[str]:
    """
    要体检的代理从哪来？
      1) 命令行直接传：python tools/diag_proxy.py 1.2.3.4:8080 5.6.7.8:9090
      2) 项目根目录的 ip_list.txt（默认，也就是爬虫正在用的那批）
    一行一个 ip:port，# 开头是注释。
    """
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return args

    if os.path.exists(IP_FILE):
        with open(IP_FILE, "r", encoding="utf-8") as f:
            lines = [x.strip() for x in f.read().splitlines()]
        ips = [x for x in lines if x and not x.startswith("#")]
        if ips:
            return ips

    return []


def tcp_probe(host: str, port: int, timeout: float = 5.0) -> Optional[str]:
    """第 1 层：TCP 端口能不能连上。返回 None 表示能连，否则返回错误描述。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return "TCP 连接超时"
    except ConnectionRefusedError:
        return "TCP 连接被拒绝（端口未开）"
    except OSError as e:
        return f"TCP {type(e).__name__}: {e}"


def http_probe(proxy: str, url: str, timeout: float = 10.0) -> str:
    """第 2 层：让代理转发一个 https 请求，看返回什么。"""
    try:
        r = requests.get(url,
                         proxies={"http": proxy, "https": proxy},
                         timeout=timeout,
                         headers={"User-Agent": UA,
                                  "Accept-Language": "zh-CN,zh;q=0.9"},
                         allow_redirects=True)
    except requests.exceptions.ProxyError as e:
        return f"✗ 代理层失败：{str(e)[:80]}"
    except requests.exceptions.SSLError as e:
        return f"✗ SSL 握手失败：{str(e)[:60]}"
    except requests.exceptions.ConnectTimeout:
        return "✗ CONNECT 超时"
    except requests.exceptions.ReadTimeout:
        return "✗ 读取超时"
    except Exception as e:
        return f"✗ {type(e).__name__}: {str(e)[:60]}"

    ok = r.status_code == 200 and "car-box" in r.text
    return (f"{'✓' if ok else '✗'} HTTP {r.status_code} | "
            f"{len(r.text)} 字符 | 含car-box={('car-box' in r.text)} | "
            f"最终 {r.url[:60]}")


def main() -> None:
    targets = load_targets()

    print("=" * 74)
    print("代理串行体检")
    print(f"名单来源：{IP_FILE if not sys.argv[1:] else '命令行参数'}")
    print(f"待体检 {len(targets)} 个")
    print("=" * 74)

    if not targets:
        print(f"\n✗ 没有要体检的代理。")
        print(f"  把 IP 一行一个写进 {IP_FILE}（格式 ip:port），")
        print(f"  或者直接传参：python tools/diag_proxy.py 1.2.3.4:8080")
        return

    alive_tcp = []
    for i, item in enumerate(targets, 1):
        if ":" not in item:
            print(f"\n[{i}/{len(targets)}] {item}  格式不对，跳过（要 ip:port）")
            continue
        host, port = item.split(":")[0], item.split(":")[-1]
        proxy = f"http://{item}"
        print(f"\n[{i}/{len(targets)}] {item}")

        t0 = time.time()
        err = tcp_probe(host, int(port))
        if err:
            print(f"    ① TCP    : ✗ {err}  ({time.time() - t0:.1f}s)")
            continue
        print(f"    ① TCP    : ✓ 端口通  ({time.time() - t0:.1f}s)")
        alive_tcp.append(proxy)

        t0 = time.time()
        r1 = http_probe(proxy, "https://www.guazi.com/dg/")
        print(f"    ② 目标站 : {r1}  ({time.time() - t0:.1f}s)")

    print("\n" + "=" * 74)
    print(f"TCP 可达：{len(alive_tcp)}/{len(targets)}")
    for p in alive_tcp:
        print(f"  {p}")
    print("=" * 74)


if __name__ == "__main__":
    main()
