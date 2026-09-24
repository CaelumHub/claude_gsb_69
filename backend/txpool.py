"""In-memory transaction pool (mempool).

The pool holds signed, validated-but-unconfirmed transactions until a block is
mined.  It enforces:

* signature validity,
* sender authenticity (public key must match the sender address),
* nonce monotonicity (only the *next* nonce per sender is admitted, preventing
  nonce gaps and making double-spends structurally impossible),
* sufficient balance against the current world state,
* fee >= 0 and a bounded pool size.

When a block is mined, included transactions are dropped; on a reorg, the
transactions from abandoned blocks are re-admitted so they are not lost.
"""

import time

from .config import TXPOOL_SORT_KEY
from .transaction import Transaction


class TxPool:
    def __init__(self, max_size=1000):
        self.max_size = max_size
        self._pool = {}          # txid -> Transaction
        self._order = []         # txids in arrival order
        self._by_sender = {}     # sender -> txid (one pending tx per sender)

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def __len__(self):
        return len(self._pool)

    def size(self):
        return len(self._pool)

    def get(self, txid):
        return self._pool.get(txid)

    def contains(self, txid):
        return txid in self._pool

    def all(self):
        return [self._pool[t] for t in self._order]

    def ordered_all(self):
        """Transactions listed for the UI in display order."""
        return sorted(self.all(),
                      key=lambda tx: getattr(tx, TXPOOL_SORT_KEY, "") or "")

    def txids(self):
        return list(self._order)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate(self, tx, world_state):
        """Return ``(ok, reason)`` for admitting ``tx`` into the pool."""
        if not isinstance(tx, Transaction):
            return False, "not a transaction"
        if tx.tx_type == "coinbase":
            return False, "coinbase transactions cannot be submitted to the pool"
        if tx.txid in self._pool:
            return False, "transaction already in pool"
        if not tx.validate_signature():
            return False, "invalid signature"
        if tx.derived_sender() != tx.sender:
            return False, "sender does not match public key"
        if tx.sender in self._by_sender:
            return False, "sender already has a pending transaction"
        if tx.fee < 0 or tx.amount < 0:
            return False, "negative fee or amount"
        expected_nonce = world_state.nonce(tx.sender)
        if tx.nonce != expected_nonce:
            return False, (f"nonce {tx.nonce} != expected {expected_nonce} "
                           f"(account nonce)")
        if tx.tx_type == "transfer":
            if not tx.to:
                return False, "transfer requires a recipient"
            if world_state.balance(tx.sender) < tx.amount + tx.fee:
                return False, "insufficient balance"
        elif tx.tx_type == "deploy":
            if world_state.balance(tx.sender) < tx.fee:
                return False, "insufficient balance for deploy fee"
        elif tx.tx_type == "call":
            if world_state.balance(tx.sender) < tx.amount + tx.fee:
                return False, "insufficient balance for call"
        else:
            return False, f"unknown transaction type '{tx.tx_type}'"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def add(self, tx):
        if tx.txid in self._pool:
            return False
        if tx.sender in self._by_sender and tx.sender not in (None, ""):
            return False
        if self.size() >= self.max_size:
            # Evict the oldest transaction to stay within bounds.
            oldest = self._order.pop(0)
            evicted = self._pool.pop(oldest, None)
            if evicted is not None:
                self._by_sender.pop(evicted.sender, None)
        self._pool[tx.txid] = tx
        self._order.append(tx.txid)
        if tx.sender:
            self._by_sender[tx.sender] = tx.txid
        return True

    def remove(self, txid):
        if txid in self._pool:
            tx = self._pool.pop(txid, None)
            if tx is not None and tx.sender:
                self._by_sender.pop(tx.sender, None)
            if txid in self._order:
                self._order.remove(txid)
            return True
        return False

    def remove_many(self, txids):
        for txid in txids:
            self.remove(txid)

    def clear(self):
        self._pool.clear()
        self._order.clear()
        self._by_sender.clear()

    def re_admit(self, transactions):
        """Re-add transactions (e.g. from an abandoned fork block)."""
        for tx in transactions:
            if tx.txid not in self._pool and not tx.is_coinbase():
                self.add(tx)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def to_list(self):
        return [tx.to_dict() for tx in self.all()]

    def load(self, data, world_state):
        self.clear()
        for d in data or []:
            tx = Transaction.from_dict(d)
            if self.validate(tx, world_state)[0]:
                self.add(tx)
