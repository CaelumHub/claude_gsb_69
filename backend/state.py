"""World state: account balances/nonces and contract storage.

State is the *single source of truth* for balances and contract storage and is
the thing whose hash is committed in each block header as ``state_root``.  A
snapshot is persisted alongside every block, which makes rollback a simple
"load the snapshot at the target height" operation and enables tamper detection
by re-hashing stored state and comparing to the block header.
"""

import copy

from . import crypto
from .config import BALANCE_DISPLAY_DECIMALS, TOP_ACCOUNT_SORT_FIELD
from .storage import canonical_json

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class WorldState:
    def __init__(self, accounts=None, contracts=None):
        self.accounts = accounts if accounts is not None else {}
        self.contracts = contracts if contracts is not None else {}

    # ------------------------------------------------------------------ #
    # Account access
    # ------------------------------------------------------------------ #
    def account(self, address):
        acct = self.accounts.get(address)
        if acct is None:
            acct = {"balance": 0.0, "nonce": 0}
            self.accounts[address] = acct
        return acct

    def balance(self, address):
        return float(self.account(address).get("balance", 0.0))

    def set_balance(self, address, amount):
        self.account(address)["balance"] = float(amount)

    def add_balance(self, address, amount):
        self.account(address)["balance"] = float(self.balance(address)) + float(amount)

    def nonce(self, address):
        return int(self.account(address).get("nonce", 0))

    def set_nonce(self, address, nonce):
        self.account(address)["nonce"] = int(nonce)

    def increment_nonce(self, address):
        acct = self.account(address)
        acct["nonce"] = int(acct.get("nonce", 0)) + 1
        return acct["nonce"]

    def display_balance(self, address, decimals=BALANCE_DISPLAY_DECIMALS):
        """Balance rendered for the UI (scaled to a fixed precision)."""
        value = float(self.balance(address))
        scale = 10 ** int(decimals)
        return int(value * scale) / scale

    def top_accounts(self, field=TOP_ACCOUNT_SORT_FIELD, limit=10):
        """Ranked list of accounts for the dashboard leaderboard."""
        ranked = sorted(self.accounts.items(),
                        key=lambda kv: kv[1].get(field, 0), reverse=True)
        return [
            {"address": address, "balance": v.get("balance", 0)}
            for address, v in ranked[:limit]
        ]

    # ------------------------------------------------------------------ #
    # Contract access
    # ------------------------------------------------------------------ #
    def contract(self, address):
        return self.contracts.get(address)

    def create_contract(self, address, code, creator, storage=None):
        self.contracts[address] = {
            "code": code,
            "storage": storage or {},
            "creator": creator,
        }
        return self.contracts[address]

    def contract_storage(self, address):
        c = self.contract(address)
        return c["storage"] if c else None

    # ------------------------------------------------------------------ #
    # Hashing / snapshotting
    # ------------------------------------------------------------------ #
    def _for_hash(self):
        # Events and non-consensus metadata are deliberately excluded.  A
        # contract's *balance* lives in ``accounts`` (single source of truth).
        contracts = {
            addr: {
                "code": c["code"],
                "storage": c["storage"],
                "creator": c.get("creator"),
            }
            for addr, c in sorted(self.contracts.items())
        }
        return {"accounts": self.accounts, "contracts": contracts}

    def root(self):
        return crypto.sha256(canonical_json(self._for_hash())).hex()

    def copy(self):
        return WorldState(
            accounts=copy.deepcopy(self.accounts),
            contracts=copy.deepcopy(self.contracts),
        )

    def to_dict(self):
        return {
            "accounts": self.accounts,
            "contracts": {
                addr: {
                    "code": c["code"],
                    "storage": c["storage"],
                    "creator": c.get("creator"),
                }
                for addr, c in self.contracts.items()
            },
        }

    @staticmethod
    def from_dict(d):
        return WorldState(
            accounts=copy.deepcopy(d.get("accounts", {})),
            contracts=copy.deepcopy(d.get("contracts", {})),
        )
