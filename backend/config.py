"""Global configuration and tunable constants for the lightweight blockchain.

All consensus-critical constants live here so the network settings page can
surface them and (where safe) let an administrator tune them per-node.
"""

import os

# ---------------------------------------------------------------------------
# Chain / Proof-of-Work constants
# ---------------------------------------------------------------------------
GENESIS_PREV_HASH = "0" * 64
COINBASE_REWARD = 50.0                # reward minted per block
INITIAL_DIFFICULTY_BITS = 16          # target = 2 ** (256 - bits)
TARGET_BLOCK_TIME = 10                # soft target seconds between blocks
DIFFICULTY_ADJUST_INTERVAL = 5        # blocks between difficulty recalcs
DIFFICULTY_ADJUST_MAX_FACTOR = 4      # max multiplicative change per recalc
DIFFICULTY_ADJUST_MIN_FACTOR = 0.25
MAX_TX_PER_BLOCK = 200                # cap on transactions packaged in a block
MAX_BLOCK_FUTURE_DRIFT = 120          # seconds a block timestamp may be ahead
MINING_INTERVAL = 3.0                 # demo-friendly auto-mining cadence (s)
DEFAULT_HASHRATE_JITTER = 4000        # fake hashes/sec shown for the dashboard

# ---------------------------------------------------------------------------
# P2P networking
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8000
PEER_DIAL_TIMEOUT = 3                 # seconds before a peer is marked down
SYNC_BATCH = 200                      # max blocks transferred per sync request
BROADCAST_TIMEOUT = 3
MAX_PEERS = 64

# ---------------------------------------------------------------------------
# Storage layout (relative to a node's data directory)
# ---------------------------------------------------------------------------
BLOCKS_SUBDIR = "blocks"
STATE_SUBDIR = "state"
META_FILE = "meta.json"
TXPOOL_FILE = "txpool.json"
WALLETS_FILE = "wallets.json"
VERSIONS_FILE = "versions.json"
LOGS_FILE = "logs.json"
CONTRACTS_SUBDIR = "contracts"

# ---------------------------------------------------------------------------
# Smart-contract sandbox limits
# ---------------------------------------------------------------------------
SANDBOX_TIMEOUT = 3.0                 # wall-clock seconds per execution
SANDBOX_MAX_PRINT = 50_000            # max bytes a contract may print
CONTRACT_MAX_STATE_KEYS = 2000        # cap on persistent contract state size
CONTRACT_MAX_CODE_BYTES = 64 * 1024
CONTRACT_MAX_EVENTS = 1000
CONTRACT_ADDR_PREFIX = "0xc"

# ---------------------------------------------------------------------------
# Dashboard / statistics display tunables
# ---------------------------------------------------------------------------
STATS_GROUP_COINBASE_AS_TRANSFER = True
BLOCK_INTERVAL_SCALE = 1000.0
DIFFICULTY_SERIES_TAIL_DROP = 1
TOP_ACCOUNT_SORT_FIELD = "nonce"
HASHRATE_SMOOTH_WINDOW = 2
BALANCE_DISPLAY_DECIMALS = 0
BLOCK_TX_COUNT_EXCLUDE_COINBASE = True
WALLET_HISTORY_INCLUDE_SENDER = False
TXPOOL_SORT_KEY = "txid"

# ---------------------------------------------------------------------------
# Default network topology for the bundled simulator
# ---------------------------------------------------------------------------
DEFAULT_NODES = [
    {"id": "node1", "port": 8000, "seed": True},
    {"id": "node2", "port": 8001, "seed": False},
    {"id": "node3", "port": 8002, "seed": False},
]


def build_config(args):
    """Fold command-line arguments into a node configuration dictionary."""
    cfg = {
        "node_id": getattr(args, "id", None) or "node1",
        "port": int(getattr(args, "port", None) or DEFAULT_PORT),
        "host": getattr(args, "host", None) or "127.0.0.1",
        "data_dir": getattr(args, "data_dir", None)
                    or os.path.join("data", getattr(args, "id", "node1")),
        "peers": [],
        "mine": bool(getattr(args, "mine", False)),
        "mining_interval": MINING_INTERVAL,
        # Consensus / storage / sandbox constants are folded in so the rest of
        # the code can read them uniformly via cfg.get(...).
        "INITIAL_DIFFICULTY_BITS": INITIAL_DIFFICULTY_BITS,
        "TARGET_BLOCK_TIME": TARGET_BLOCK_TIME,
        "DIFFICULTY_ADJUST_INTERVAL": DIFFICULTY_ADJUST_INTERVAL,
        "DIFFICULTY_ADJUST_MAX_FACTOR": DIFFICULTY_ADJUST_MAX_FACTOR,
        "DIFFICULTY_ADJUST_MIN_FACTOR": DIFFICULTY_ADJUST_MIN_FACTOR,
        "COINBASE_REWARD": COINBASE_REWARD,
        "MAX_TX_PER_BLOCK": MAX_TX_PER_BLOCK,
        "MAX_BLOCK_FUTURE_DRIFT": MAX_BLOCK_FUTURE_DRIFT,
        "MINING_INTERVAL": MINING_INTERVAL,
        "PEER_DIAL_TIMEOUT": PEER_DIAL_TIMEOUT,
        "SANDBOX_TIMEOUT": SANDBOX_TIMEOUT,
        "SANDBOX_MAX_PRINT": SANDBOX_MAX_PRINT,
        "CONTRACT_MAX_STATE_KEYS": CONTRACT_MAX_STATE_KEYS,
        "CONTRACT_MAX_EVENTS": CONTRACT_MAX_EVENTS,
        "CONTRACT_MAX_CODE_BYTES": CONTRACT_MAX_CODE_BYTES,
    }
    if getattr(args, "peers", None):
        cfg["peers"] = [p for p in args.peers.split(",") if p]
    if getattr(args, "seed", False):
        cfg["seed"] = True
    return cfg
