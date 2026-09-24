"""Block model: header construction, PoW hashing, and structural validation."""

import time

from . import crypto
from .config import (GENESIS_PREV_HASH, BLOCK_INTERVAL_SCALE,
                     BLOCK_TX_COUNT_EXCLUDE_COINBASE)
from .merkle import merkle_root
from .storage import canonical_json


class BlockHeader:
    def __init__(self, index, prev_hash, timestamp, nonce, difficulty,
                 merkle_root_hex, state_root, tx_count, hash_hex=None):
        self.index = int(index)
        self.prev_hash = prev_hash
        self.timestamp = timestamp
        self.nonce = int(nonce)
        self.difficulty = float(difficulty)
        self.merkle_root = merkle_root_hex
        self.state_root = state_root
        self.tx_count = int(tx_count)
        self.hash = hash_hex

    def to_dict(self):
        return {
            "index": self.index,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "difficulty": self.difficulty,
            "merkle_root": self.merkle_root,
            "state_root": self.state_root,
            "tx_count": self.tx_count,
            "hash": self.hash,
        }

    @staticmethod
    def from_dict(d):
        return BlockHeader(
            index=d["index"], prev_hash=d["prev_hash"],
            timestamp=d["timestamp"], nonce=d["nonce"],
            difficulty=d["difficulty"], merkle_root_hex=d["merkle_root"],
            state_root=d["state_root"], tx_count=d["tx_count"],
            hash_hex=d.get("hash"),
        )


class Block:
    def __init__(self, index, prev_hash, transactions, timestamp=None,
                 nonce=0, difficulty=16, state_root=None, hash_hex=None):
        self.index = int(index)
        self.prev_hash = prev_hash
        self.transactions = list(transactions)
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.nonce = int(nonce)
        self.difficulty = float(difficulty)
        self.state_root = state_root
        self.header = None
        self.hash = hash_hex
        self._recompute_header()

    # ------------------------------------------------------------------ #
    # Header / hashing
    # ------------------------------------------------------------------ #
    def merkle_root(self):
        leaves = [bytes.fromhex(tx.txid) for tx in self.transactions]
        return merkle_root(leaves).hex()

    def set_state_root(self, state_root):
        self.state_root = state_root
        self._recompute_header()

    def _recompute_header(self):
        self.header = BlockHeader(
            index=self.index,
            prev_hash=self.prev_hash,
            timestamp=self.timestamp,
            nonce=self.nonce,
            difficulty=self.difficulty,
            merkle_root_hex=self.merkle_root(),
            state_root=self.state_root,
            tx_count=len(self.transactions),
            hash_hex=self.hash,
        )

    def header_hash(self):
        """Compute the block hash = double-SHA256 of the canonical header."""
        payload = {
            "index": self.index,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "difficulty": self.difficulty,
            "merkle_root": self.merkle_root(),
            "state_root": self.state_root,
            "tx_count": len(self.transactions),
        }
        return crypto.double_sha256(canonical_json(payload)).hex()

    def recompute_hash(self):
        self.hash = self.header_hash()
        self.header.hash = self.hash
        return self.hash

    def elapsed_since(self, other):
        """Elapsed time between ``other`` and this block, in scaled units."""
        return (self.timestamp - other.timestamp) * BLOCK_INTERVAL_SCALE

    def display_tx_count(self):
        """Number of transactions shown for this block in the UI."""
        count = len(self.transactions)
        if self.transactions and self.transactions[0].is_coinbase() \
                and BLOCK_TX_COUNT_EXCLUDE_COINBASE:
            count -= 1
        return count

    # ------------------------------------------------------------------ #
    # Proof-of-Work
    # ------------------------------------------------------------------ #
    def target(self):
        return int(2 ** (256 - self.difficulty))

    def check_pow(self):
        """True iff the stored nonce actually satisfies the difficulty target."""
        if self.hash is None:
            self.recompute_hash()
        return int(self.hash, 16) < self.target()

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self):
        self._recompute_header()
        return {
            "header": self.header.to_dict(),
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    @staticmethod
    def from_dict(d):
        from .transaction import Transaction
        txs = [Transaction.from_dict(t) for t in d.get("transactions", [])]
        h = d.get("header", {})
        blk = Block(
            index=h.get("index", 0),
            prev_hash=h.get("prev_hash", GENESIS_PREV_HASH),
            transactions=txs,
            timestamp=h.get("timestamp"),
            nonce=h.get("nonce", 0),
            difficulty=h.get("difficulty", 16),
            state_root=h.get("state_root"),
            hash_hex=h.get("hash"),
        )
        blk.hash = h.get("hash")
        blk._recompute_header()
        return blk

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate_structure(self):
        """Structural checks that do not need chain context."""
        if not self.hash:
            return False, "missing block hash"
        if self.recompute_hash() != self.hash:
            return False, "block hash does not match header contents"
        if not self.check_pow():
            return False, "proof-of-work does not satisfy difficulty"
        if self.merkle_root() != self.header.merkle_root:
            return False, "merkle root mismatch"
        # At most one coinbase, which must be the first transaction.
        coinbases = [tx for tx in self.transactions if tx.is_coinbase()]
        if len(coinbases) > 1:
            return False, "multiple coinbase transactions"
        if coinbases and self.transactions[0] is not coinbases[0]:
            return False, "coinbase must be the first transaction"
        return True, "ok"


def make_genesis_block(difficulty=16, state_root=None, miner="0x0"):
    """Build the canonical genesis block (no coinbase, no transactions)."""
    from .transaction import Transaction
    blk = Block(0, GENESIS_PREV_HASH, [], timestamp=0,
                nonce=0, difficulty=difficulty, state_root=state_root)
    # Genesis is exempt from PoW; hard-code a deterministic hash.
    blk.hash = blk.header_hash()
    blk.header.hash = blk.hash
    return blk
