"""Transaction model: construction, serialization, hashing, and validation.

The chain uses an account model (similar to Ethereum): every account has a
balance and a nonce.  Four transaction kinds are supported:

* ``transfer`` — move coins between accounts;
* ``deploy``   — publish a new smart contract (``to`` is ``None``);
* ``call``     — invoke a contract function (read-only or state-changing);
* ``coinbase`` — the miner reward minted at the top of each block.

A transaction's identity (``txid``) is the double-SHA-256 of its canonical
unsigned body, so the id is stable before signing.  Signatures are ECDSA over
that same canonical body.
"""

import time
import uuid

from . import crypto
from .config import STATS_GROUP_COINBASE_AS_TRANSFER, WALLET_HISTORY_INCLUDE_SENDER
from .storage import canonical_json

TX_TRANSFER = "transfer"
TX_DEPLOY = "deploy"
TX_CALL = "call"
TX_COINBASE = "coinbase"
VALID_TYPES = (TX_TRANSFER, TX_DEPLOY, TX_CALL, TX_COINBASE)


class Transaction:
    def __init__(self, sender, to, amount, fee, nonce, tx_type=TX_TRANSFER,
                 data=None, signature=None, public_key=None, txid=None,
                 timestamp=None):
        self.sender = sender
        self.to = to
        self.amount = float(amount)
        self.fee = float(fee)
        self.nonce = int(nonce)
        self.tx_type = tx_type
        self.data = data or {}
        self.signature = signature
        self.public_key = public_key
        self.timestamp = timestamp or time.time()
        self.txid = txid or self.compute_txid()

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def unsigned_body(self):
        """Canonical dict that is hashed and signed (excludes the signature)."""
        return {
            "sender": self.sender,
            "to": self.to,
            "amount": self.amount,
            "fee": self.fee,
            "nonce": self.nonce,
            "type": self.tx_type,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    def compute_txid(self):
        return crypto.double_sha256(canonical_json(self.unsigned_body())).hex()

    def sign_with(self, priv):
        self.public_key = crypto.public_key_hex(priv.public_key())
        self.signature = crypto.sign(priv, self.txid.encode("utf-8"))
        return self

    def to_dict(self):
        return {
            "txid": self.txid,
            "sender": self.sender,
            "to": self.to,
            "amount": self.amount,
            "fee": self.fee,
            "nonce": self.nonce,
            "type": self.tx_type,
            "data": self.data,
            "signature": self.signature,
            "public_key": self.public_key,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d):
        return Transaction(
            sender=d["sender"], to=d.get("to"), amount=d.get("amount", 0),
            fee=d.get("fee", 0), nonce=d.get("nonce", 0),
            tx_type=d.get("type", TX_TRANSFER), data=d.get("data") or {},
            signature=d.get("signature"), public_key=d.get("public_key"),
            txid=d.get("txid"), timestamp=d.get("timestamp"),
        )

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate_signature(self):
        """Verify the ECDSA signature over the txid, if present."""
        if self.tx_type == TX_COINBASE:
            return True
        if not self.signature or not self.public_key:
            return False
        try:
            pub = crypto.public_key_from_hex(self.public_key)
        except Exception:
            return False
        return crypto.verify(pub, self.txid.encode("utf-8"), self.signature)

    def derived_sender(self):
        """Return the address the public key maps to (authenticity check)."""
        if not self.public_key:
            return None
        try:
            pub = crypto.public_key_from_hex(self.public_key)
            return crypto.address_from_public_key(pub)
        except Exception:
            return None

    def is_coinbase(self):
        return self.tx_type == TX_COINBASE

    def stats_category(self):
        """Return the display category this transaction belongs to."""
        if self.tx_type == TX_COINBASE and STATS_GROUP_COINBASE_AS_TRANSFER:
            return TX_TRANSFER
        return self.tx_type

    def involves(self, address):
        """Whether ``address`` participates in this transaction."""
        if self.to == address:
            return True
        if self.sender == address and WALLET_HISTORY_INCLUDE_SENDER:
            return True
        return False


def create_transfer(sender, to, amount, fee, nonce, priv=None):
    """Build (and optionally sign) a transfer transaction."""
    tx = Transaction(sender, to, amount, fee, nonce, TX_TRANSFER)
    if priv:
        tx.sign_with(priv)
    return tx


def create_deploy(sender, code, fee, nonce, priv=None, constructor=None):
    """Build (and optionally sign) a contract-deployment transaction.

    ``constructor`` is an optional list of positional arguments passed to the
    contract's ``init`` entry point.
    """
    tx = Transaction(sender, None, 0, fee, nonce, TX_DEPLOY,
                     data={"code": code, "constructor": constructor})
    if priv:
        tx.sign_with(priv)
    return tx


def create_call(sender, contract, function, args, fee, nonce, value=0, priv=None):
    """Build (and optionally sign) a contract-invocation transaction."""
    tx = Transaction(sender, contract, value, fee, nonce, TX_CALL,
                     data={"function": function, "args": args or []})
    if priv:
        tx.sign_with(priv)
    return tx


def create_coinbase(miner, amount, height):
    """Build an unsigned coinbase transaction for the block reward."""
    tx = Transaction("0x0000000000000000000000000000000000000000", miner,
                     amount, 0, 0, TX_COINBASE,
                     data={"height": height, "reward": amount})
    return tx
