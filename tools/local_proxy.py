# -*- coding: utf-8 -*-
"""
本地自建 HTTP/HTTPS 代理（验证 IP 池链路专用）
================================================
作用：在你自己的机器上开几个本地代理端口，当作"假的可用 IP"，
      用来验证「IP 池 → 每页换 IP → 抓取成功」这条链路是通的。
      它只做 TCP 转发，不缓存、不改内容。

为什么需要它：
  你手上那 10 个免费 IP 全部 TCP 不可达（已死）。没有可用 IP 时，
  IP 池代码究竟对不对是验证不出来的。用本地代理就能先把代码链路跑通，
  IP 源的问题再单独解决。

用法：
    python local_proxy.py                  # 同时开 18080 / 18081 两个端口
    python local_proxy.py 19001 19002      # 指定端口

然后把这两行写进 ip_list.txt：
    127.0.0.1:18080
    127.0.0.1:18081

再跑：
    python 瓜子二手车_dg.py --engine requests --no-manual --city dg --proxy --pages 2 --verbose

注意：这是"验证用"的代理，不是用来绕过反爬的——它的出口 IP 还是你自己。
"""

from __future__ import annotations

import socket
import sys
import threading

DEFAULT_PORTS = [18080, 18081]
BUF = 65536


def _pipe(src: socket.socket, dst: socket.socket, tag: str) -> None:
    """单向搬运数据，直到一端关闭。"""
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle_client(client: socket.socket, addr, port: int) -> None:
    """处理一个连接：CONNECT 建隧道 / 普通请求直接转发。"""
    try:
        client.settimeout(20)
        head = client.recv(BUF)
        if not head:
            return

        first_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first_line.split()
        if len(parts) < 3:
            client.close()
            return
        method, target = parts[0].upper(), parts[1]

        if method == "CONNECT":
            # ---- HTTPS：目标形如 www.guazi.com:443，建隧道后双向透传 ----
            host, _, port_s = target.rpartition(":")
            if not host:
                client.close()
                return
            upstream = socket.create_connection((host, int(port_s)), timeout=15)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            print(f"[{port}] CONNECT {target}", flush=True)
            t = threading.Thread(target=_pipe, args=(upstream, client, "up"), daemon=True)
            t.start()
            _pipe(client, upstream, "down")
            t.join(timeout=20)
            return

        # ---- HTTP：target 是完整 URL，转成路径后转发 ----
        print(f"[{port}] {method} {target}", flush=True)
        if target.startswith("http://"):
            rest = target[len("http://"):]
            hostport, _, path = rest.partition("/")
            host, _, port_s = hostport.partition(":")
            upstream = socket.create_connection((host, int(port_s or 80)), timeout=15)
            new_head = head.replace(target.encode("latin-1", "replace"),
                                    ("/" + path).encode("latin-1", "replace"), 1)
            upstream.sendall(new_head)
            client.settimeout(None)
            upstream.settimeout(None)
            t = threading.Thread(target=_pipe, args=(upstream, client, "up"), daemon=True)
            t.start()
            _pipe(client, upstream, "down")
            t.join(timeout=20)
            return

        client.close()
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[{port}] 异常 {type(e).__name__}: {e}", flush=True)
    finally:
        try:
            client.close()
        except OSError:
            pass


def serve(port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    print(f"✓ 本地代理已监听 127.0.0.1:{port}", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle_client,
                         args=(conn, addr, port), daemon=True).start()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ports = [int(a) for a in args] if args else DEFAULT_PORTS

    print("=" * 60)
    print("本地自建代理（验证 IP 池链路用）")
    print("=" * 60)
    for p in ports:
        threading.Thread(target=serve, args=(p,), daemon=True).start()

    print("\n把下面几行写进 ip_list.txt：")
    for p in ports:
        print(f"  127.0.0.1:{p}")
    print("\n然后跑：")
    print("  python 瓜子二手车_dg.py --engine requests --no-manual --city dg "
          "--proxy --pages 2 --verbose")
    print("\nCtrl+C 停止\n" + "=" * 60)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n本地代理已停止")


if __name__ == "__main__":
    main()
