# -*- coding: utf-8 -*-
"""
代理 API 拉取器（熊猫代理 / 任意返回 ip:port 列表的 API）
==========================================================
把 API 链接放进 api_url.txt（一行），运行本脚本即可：
    1) 请求 API
    2) 解析出 ip:port 列表（兼容 txt / json 两种返回）
    3) 写入 ip_list.txt（供 ip_pool.py 和爬虫使用）
    4) 可选：立刻做一次可用性检测

用法：
    python proxy_api.py                 # 拉取 → 覆盖写入 ip_list.txt
    python proxy_api.py --append        # 追加到 ip_list.txt（不覆盖原有）
    python proxy_api.py --check         # 拉完立刻检测可用性
    python proxy_api.py --dry-run       # 只打印解析结果，不写文件
    python proxy_api.py --file other.txt # 写到别的文件

关于 API 链接的安全：
    api_url.txt 里含你的 secret，不要传到公开仓库；用完可以删掉，
    也可以在系统里设置环境变量 PROXY_API_URL 来代替这个文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import List

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URL_FILE = os.path.join(BASE_DIR, "api_url.txt")
IP_FILE = os.path.join(BASE_DIR, "ip_list.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def read_api_url() -> str:
    """优先用环境变量 PROXY_API_URL，其次读 api_url.txt。"""
    env = os.environ.get("PROXY_API_URL", "").strip()
    if env:
        return env
    if os.path.exists(URL_FILE):
        with open(URL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    raise SystemExit(f"✗ 没找到 API 链接：请把链接写进 {URL_FILE}，"
                     f"或设置环境变量 PROXY_API_URL")


def fetch(url: str) -> str:
    """请求 API，返回原始响应文本。"""
    r = requests.get(url, timeout=20, headers={"User-Agent": UA})
    if r.status_code != 200:
        raise SystemExit(f"✗ API 返回 HTTP {r.status_code}：{r.text[:300]}")
    # 熊猫返回可能是文本也可能带 BOM，统一去掉
    return r.text.replace("\ufeff", "").strip()


def parse(text: str) -> List[str]:
    """
    从返回内容里抽出代理列表。兼容三种常见格式：
      A) 纯文本，一行一个 ip:port
      B) 纯文本，一行一个 user:pass@ip:port
      C) JSON，形如 {"code":0,"data":{"proxy_list":[...]}} 或 data 是列表
    返回统一成 requests 可用的 'http://...'。
    """
    raw = (text or "").strip()
    if not raw:
        return []

    items: List[str] = []

    # ---- JSON ----
    if raw.startswith("{") or raw.startswith("["):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            found = _walk_json(obj)
            items.extend(found)
            if not items:
                # 有些服务商在 code!=0 时返回错误信息，原样抛出
                msg = obj.get("msg") or obj.get("message") if isinstance(obj, dict) else None
                if msg:
                    raise SystemExit(f"✗ API 返回错误：{msg}")

    # ---- 纯文本 ----
    if not items:
        for line in re.split(r"[\r\n,;]+", raw):
            line = line.strip().strip('"').strip("'")
            if not line or line.startswith("#"):
                continue
            # 只接受含 ip:port 形态的行，过滤掉提示语
            if re.search(r"\d+\.\d+\.\d+\.\d+:\d+", line):
                items.append(line)

    out: List[str] = []
    for it in items:
        it = it.strip()
        if not it:
            continue
        if it.startswith(("http://", "https://")):
            out.append(it)
        elif re.search(r"\d+\.\d+\.\d+\.\d+:\d+", it):
            out.append("http://" + it)
    return out


def _walk_json(obj) -> List[str]:
    """
    在 JSON 里递归寻找「ip:port」字符串。
    服务商字段名五花八门（proxy_list / proxies / data / ip_list…），
    与其逐个适配，不如把树里所有长得像 ip:port 的字符串全捞出来。
    """
    found: List[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            if re.search(r"\d+\.\d+\.\d+\.\d+:\d+", node):
                found.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def write_ips(ips: List[str], path: str, append: bool = False) -> None:
    """把 IP 写进名单文件。append=False 时整体覆盖（保留注释头）。"""
    header = [
        "# IP 池列表：一行一个，格式 ip:port",
        "# 由 proxy_api.py 于拉取时自动生成\n",
    ]
    if append and os.path.exists(path):
        with open(path, "a", encoding="utf-8") as f:
            for ip in ips:
                f.write(ip.replace("http://", "") + "\n")
        print(f"✓ 已追加 {len(ips)} 个 IP 到 {path}")
        return

    with open(path, "w", encoding="utf-8") as f:
        f.writer.writelines if False else None      # noqa: 占位，避免误用
        for line in header:
            f.write(line)
        for ip in ips:
            f.write(ip.replace("http://", "") + "\n")
    print(f"✓ 已写入 {len(ips)} 个 IP 到 {path}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="从代理服务商 API 拉取 IP 名单")
    ap.add_argument("--append", action="store_true", help="追加到名单文件而不是覆盖")
    ap.add_argument("--file", default=IP_FILE, help="写入的名单文件，默认 ip_list.txt")
    ap.add_argument("--check", action="store_true", help="拉取后立刻检测可用性")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    url = read_api_url()
    print("=" * 66)
    print("代理 API 拉取")
    # 打印时隐去 secret，避免截图/日志泄露
    shown = re.sub(r"(secret=)[^&]+", r"\1<已隐藏>", url)
    print(f"链接：{shown}")
    print("=" * 66)

    text = fetch(url)
    print(f"\n原始返回 {len(text)} 字符：")
    for line in text.splitlines()[:15]:
        print("  " + line)
    if len(text.splitlines()) > 15:
        print(f"  …（共 {len(text.splitlines())} 行）")

    ips = parse(text)
    print(f"\n解析到 {len(ips)} 个代理：")
    for ip in ips:
        print("  " + ip)

    if not ips:
        print("\n✗ 没解析出代理。可能原因：订单已过期 / 提取次数用完 / 需要绑定白名单 IP")
        return

    if args.dry_run:
        print("\n[dry-run] 未写文件")
        return

    write_ips(ips, args.file, append=args.append)

    if args.check:
        print()
        import ip_pool as IP
        results = IP.check_all([f"http://{x.replace('http://', '')}" for x in ips])
        good = [p for p, ok, _ in results if ok]
        print(f"\n检测结果：{len(good)}/{len(ips)} 可用")
        for p in good:
            print("  ✓ " + p)


if __name__ == "__main__":
    main()
