"""Local wallet management: key generation, import, listing, signing.

Wallets are stored on disk as ``wallets.json`` mapping address -> private key
(hex), public key (hex), label and creation time.  Private keys never leave the
node over the API by default; export is an explicit, admin-gated operation.
"""

import time

from . import crypto
from .storage import atomic_write_json, read_json


class WalletStore:
    def __init__(self, path):
        self.path = path
        self.wallets = read_json(path, {})

    def _save(self):
        atomic_write_json(self.path, self.wallets)

    def create(self, label=None):
        priv = crypto.generate_private_key()
        addr = crypto.address_from_private_key(priv)
        self.wallets[addr] = {
            "private_key": crypto.private_key_to_hex(priv),
            "public_key": crypto.public_key_hex(priv.public_key()),
            "label": label or "",
            "created": time.time(),
        }
        self._save()
        return addr, self.wallets[addr]

    def import_private_key(self, priv_hex, label=None):
        try:
            priv = crypto.private_key_from_hex(priv_hex)
        except Exception:
            return None, "invalid private key"
        addr = crypto.address_from_private_key(priv)
        self.wallets[addr] = {
            "private_key": priv_hex.lower(),
            "public_key": crypto.public_key_hex(priv.public_key()),
            "label": label or "",
            "created": time.time(),
        }
        self._save()
        return addr, self.wallets[addr]

    def list(self):
        return [
            {
                "address": addr,
                "public_key": w["public_key"],
                "label": w.get("label", ""),
                "created": w.get("created"),
            }
            for addr, w in self.wallets.items()
        ]

    def get(self, address):
        return self.wallets.get(address)

    def has(self, address):
        return address in self.wallets

    def private_key(self, address):
        w = self.wallets.get(address)
        if not w:
            return None
        try:
            return crypto.private_key_from_hex(w["private_key"])
        except Exception:
            return None

    def export_private_key(self, address):
        w = self.wallets.get(address)
        return w["private_key"] if w else None

    def set_label(self, address, label):
        if address in self.wallets:
            self.wallets[address]["label"] = label
            self._save()
            return True
        return False

    def sign_message(self, address, message):
        priv = self.private_key(address)
        if priv is None:
            return None, "wallet not found locally"
        data = message.encode("utf-8")
        return crypto.sign(priv, data), None
