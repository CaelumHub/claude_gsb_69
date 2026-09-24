#!/usr/bin/env python3
"""Launcher for a single blockchain node.

Examples
--------
    python3 run.py                              # seed node on :8000 (auto-mine)
    python3 run.py --id node2 --port 8001 \
        --peers 127.0.0.1:8000                  # join node1
    python3 run.py --id node3 --port 8002 \
        --peers 127.0.0.1:8000,127.0.0.1:8001

Use ``start_network.sh`` to bring up a whole 3-node network at once.
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.config import build_config
from backend.node import Node
from backend.server import create_app


def main():
    parser = argparse.ArgumentParser(description="Lightweight blockchain node")
    parser.add_argument("--id", help="node id (default: node1)")
    parser.add_argument("--port", type=int, help="HTTP port")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--peers", help="comma-separated host:port peers")
    parser.add_argument("--data-dir", help="data directory for this node")
    parser.add_argument("--mine", action="store_true",
                        help="start auto-mining on boot")
    parser.add_argument("--seed", action="store_true",
                        help="mark this node as a seed")
    parser.add_argument("--no-mine", action="store_true",
                        help="disable auto-mining")
    parser.add_argument("--mining-interval", type=float,
                        help="seconds between auto-mined blocks")
    args = parser.parse_args()

    cfg = build_config(args)
    if args.no_mine:
        cfg["mine"] = False
    elif args.mine or not getattr(args, "peers", None):
        # A standalone or seed node mines by default.
        cfg["mine"] = True

    node = Node(cfg)
    node.start()

    app = create_app(node)
    print(f"[{node.node_id}] listening on http://{args.host}:{cfg['port']} "
          f"(height {node.blockchain.height})", flush=True)
    app.run(host=cfg["host"], port=cfg["port"], threaded=True, debug=False,
            use_reloader=False)


if __name__ == "__main__":
    main()
