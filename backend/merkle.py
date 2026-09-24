"""Merkle tree construction, root computation, and membership proofs.

The block header commits to a Merkle root over the serialized transaction ids,
which lets a light client verify that a transaction belongs to a block without
downloading the entire block.  Proofs are expressed as a list of
``(direction, sibling_hash)`` steps from the leaf to the root.
"""

import hashlib

EMPTY_ROOT = b"\x00" * 32


def _hash(data: bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).digest()


def _pair(left: bytes, right: bytes) -> bytes:
    return _hash(left + right)


def merkle_root(leaves) -> bytes:
    """Return the 32-byte Merkle root for an iterable of leaf bytes.

    An empty tree yields the fixed ``EMPTY_ROOT``.  An odd-width level is
    promoted by duplicating its last element (Bitcoin-style).
    """
    level = [l if isinstance(l, bytes) else bytes(l) for l in leaves]
    if not level:
        return EMPTY_ROOT
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


class MerkleTree:
    """A Merkle tree that retains its levels for proof generation."""

    def __init__(self, leaves):
        self.leaves = [l if isinstance(l, bytes) else bytes(l) for l in leaves]
        self.root = merkle_root(self.leaves)
        self._levels = []
        level = self.leaves
        if level:
            self._levels.append(level)
            while len(level) > 1:
                if len(level) % 2 == 1:
                    level = level + [level[-1]]
                level = [_pair(level[i], level[i + 1])
                         for i in range(0, len(level), 2)]
                self._levels.append(level)

    def proof(self, index: int):
        """Return an inclusion proof for the leaf at ``index``.

        Each step is a dict ``{"dir": "L"|"R", "hash": hex}`` where ``dir`` is
        the side the sibling occupies relative to the current node.
        """
        if not self._levels:
            return []
        if not 0 <= index < len(self.leaves):
            raise IndexError("leaf index out of range")
        proof = []
        for level in self._levels[:-1]:
            is_left = index % 2 == 0
            sibling = level[index + 1] if is_left else level[index - 1]
            proof.append({
                "dir": "R" if is_left else "L",
                "hash": sibling.hex(),
            })
            index //= 2
        return proof

    @staticmethod
    def verify(leaf: bytes, index: int, proof, root: bytes) -> bool:
        """Recompute the root from a leaf and its proof, comparing to ``root``."""
        node = leaf if isinstance(leaf, bytes) else bytes(leaf)
        for step in proof:
            sibling = bytes.fromhex(step["hash"])
            if step["dir"] == "R":
                node = _pair(node, sibling)
            else:
                node = _pair(sibling, node)
        return node == root
