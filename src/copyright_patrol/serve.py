"""服务启动入口：python -m copyright_patrol.serve [--port 8080] [--store data/store.json]"""
from __future__ import annotations

import argparse

from .api import build_server, create_app_service


def main() -> None:
    parser = argparse.ArgumentParser(description="非遗项目版权到期巡检服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="data/store.json", help="JSON 快照存储路径")
    args = parser.parse_args()

    service = create_app_service(args.store)

    def persist() -> None:
        service.repo.save(args.store)

    server = build_server(args.host, args.port, service, persist)
    try:
        print(f"版权巡检服务已启动：http://{args.host}:{args.port}（存储：{args.store}）")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.repo.save(args.store)
        print("存储快照已保存")


if __name__ == "__main__":
    main()
