"""P2P networking: peer registry and node-to-node HTTP transport.

Nodes communicate over plain HTTP (``requests``).  Each node exposes a small
``/p2p/*`` surface (status, blocks, block, tx, chain-head) and knows a list of
peers.  Broadcasting a new block/transaction fans it out to every reachable
peer; conflict resolution happens on the receiving node via the blockchain's
fork/reorg rules rather than by trusting the sender.
"""

import time

import requests

from .config import PEER_DIAL_TIMEOUT

# Message type tags used for logging / UI.
MSG_STATUS = "status"
MSG_BLOCK = "block"
MSG_TX = "tx"
MSG_BLOCKS = "blocks"
MSG_HEAD = "head"


class Peer:
    def __init__(self, peer_id, host, port):
        self.id = peer_id or f"peer-{port}"
        self.host = host
        self.port = int(port)
        self.url = f"http://{host}:{port}"
        self.status = "unknown"
        self.height = None
        self.head_hash = None
        self.chainwork = None
        self.last_seen = 0
        self.latency_ms = None
        self.last_error = None

    def to_dict(self):
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "status": self.status,
            "height": self.height,
            "head_hash": self.head_hash,
            "chainwork": self.chainwork,
            "last_seen": self.last_seen,
            "latency_ms": self.latency_ms,
            "last_error": self.last_error,
        }


class PeerRegistry:
    def __init__(self):
        self.peers = {}          # "host:port" -> Peer
        self._log = []

    def add(self, peer_id, host, port):
        key = f"{host}:{port}"
        if key not in self.peers:
            self.peers[key] = Peer(peer_id, host, port)
        return self.peers[key]

    def remove(self, host, port):
        key = f"{host}:{port}"
        return self.peers.pop(key, None) is not None

    def all(self):
        return list(self.peers.values())

    def by_key(self, host, port):
        return self.peers.get(f"{host}:{port}")

    def known_urls(self):
        return [p.url for p in self.peers.values()]


def http_get_json(url, timeout=PEER_DIAL_TIMEOUT):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def http_post_json(url, payload, timeout=PEER_DIAL_TIMEOUT):
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def dial_peer(peer):
    """Fetch a peer's status, updating its bookkeeping fields."""
    started = time.time()
    try:
        data = http_get_json(f"{peer.url}/p2p/status", timeout=PEER_DIAL_TIMEOUT)
        peer.status = "up"
        peer.height = data.get("height")
        peer.head_hash = data.get("head_hash")
        peer.chainwork = data.get("chainwork")
        peer.last_seen = time.time()
        peer.latency_ms = int((time.time() - started) * 1000)
        peer.last_error = None
        return True, data
    except Exception as e:  # noqa: BLE001
        peer.status = "down"
        peer.last_seen = time.time()
        peer.latency_ms = None
        peer.last_error = str(e)[:200]
        return False, {"error": str(e)}
