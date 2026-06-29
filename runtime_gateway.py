"""
openvurp Runtime Gateway — entrypoint standalone.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
from core.runtime_api import RuntimeAPIServer


def main() -> int:
    parser = argparse.ArgumentParser(description="openvurp runtime gateway")
    parser.add_argument("--host", default=getattr(config, "GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(getattr(config, "GATEWAY_PORT", 8421) or 8421))
    args = parser.parse_args()

    server = RuntimeAPIServer(ROOT, host=args.host, port=args.port)
    server.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
