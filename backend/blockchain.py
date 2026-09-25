"""The blockchain: block application, fork resolution, reorg, and persistence.

This is the consensus core.  It owns the main chain (an in-memory list of
:class:`~block.Block`), the current :class:`~state.WorldState`, and a side-branch
store used for fork handling.  Consensus follows the *heaviest chain* rule:
when two competing branches exist, the one with greater cumulative work wins,
and the node re-organises onto it (rolling back blocks below the fork point and
re-applying the winning branch).

Persistence is per-block: ``blocks/NNNNNN.json`` holds a block and
``state/NNNNNN.json`` holds the world state *after* that height, both written
atomically and mirrored by a version ledger for rollback.
"""

import os
import time

from . import crypto, pow as pow_mod
from .block import Block, make_genesis_block
from .config import COINBASE_REWARD, GENESIS_PREV_HASH
from .contract import ContractEngine
from .state import WorldState, ZERO_ADDRESS
from .storage import (DataPaths, VersionLedger, atomic_write_json, read_json)
from .transaction import Transaction, TX_COINBASE


class ChainValidationError(Exception):
    pass


class Blockchain:
    def __init__(self, cfg, paths: DataPaths):
        self.cfg = cfg
        self.paths = paths
        self.chain = []                 # main-chain blocks, index 0..height
        self.state = WorldState()
        self.fork_store = {}            # height -> list of competing blocks
        self.genesis_difficulty = float(cfg.get("INITIAL_DIFFICULTY_BITS", 16))
        self.chainwork = 0              # cumulative work of the main chain
        self.engine = ContractEngine(cfg)
        self.versions = VersionLedger(paths.versions_path)
        self.last_abandoned = []
        self.last_receipts = []
        self._loaded = False
        # Caches used purely by read-only history queries; never consulted by
        # consensus.  Invalidated whenever the main chain changes.
        self._creation_height_cache = {}
        self._receipts_cache = {}

    # ==================================================================== #
    # Basic accessors
    # ==================================================================== #
    @property
    def height(self):
        return len(self.chain) - 1 if self.chain else -1

    @property
    def head(self):
        return self.chain[-1] if self.chain else None

    def get_block(self, height):
        if 0 <= height < len(self.chain):
            return self.chain[height]
        return None

    def get_block_by_hash(self, hash_hex):
        for b in self.chain:
            if b.hash == hash_hex:
                return b
        for blocks in self.fork_store.values():
            for b in blocks:
                if b.hash == hash_hex:
                    return b
        return None

    def has_block(self, hash_hex):
        return self.get_block_by_hash(hash_hex) is not None

    def cumulative_work_of(self, blocks):
        return sum(int(2 ** b.difficulty) for b in blocks)

    # ==================================================================== #
    # Bootstrap / persistence
    # ==================================================================== #
    def create_genesis(self, state_root=None):
        genesis = make_genesis_block(self.genesis_difficulty, state_root)
        self.chain = [genesis]
        self.state = WorldState()
        genesis.set_state_root(self.state.root())
        genesis.recompute_hash()
        self.chainwork = int(2 ** genesis.difficulty)
        self._persist_block(genesis, self.state)
        self._write_meta()
        self.versions.record(0, genesis.hash)
        self._loaded = True
        return genesis

    def load(self):
        """Load the chain from disk (or create genesis if absent)."""
        meta = read_json(self.paths.meta_path)
        if not meta or not os.path.exists(self.paths.block_path(0)):
            self.create_genesis()
            return

        height = int(meta.get("height", 0))
        self.genesis_difficulty = float(meta.get("genesis_difficulty",
                                                 self.genesis_difficulty))
        blocks = []
        for h in range(0, height + 1):
            data = read_json(self.paths.block_path(h))
            if data is None:
                raise ChainValidationError(f"missing block file for height {h}")
            blocks.append(Block.from_dict(data))
        # Recompute hashes defensively and verify linkage.
        for i, b in enumerate(blocks):
            b.recompute_hash()
            if i > 0 and b.prev_hash != blocks[i - 1].hash:
                raise ChainValidationError(
                    f"chain linkage broken at height {i}")
        self.chain = blocks
        state_data = read_json(self.paths.state_path(height))
        self.state = WorldState.from_dict(state_data) if state_data else WorldState()
        self.chainwork = self.cumulative_work_of(blocks)
        self._loaded = True

    def _write_meta(self):
        meta = {
            "height": self.height,
            "head_hash": self.head.hash if self.head else None,
            "genesis_difficulty": self.genesis_difficulty,
            "chainwork": self.chainwork,
            "node_id": self.cfg.get("node_id", "node"),
            "updated_at": time.time(),
        }
        atomic_write_json(self.paths.meta_path, meta)

    def _persist_block(self, block, state):
        atomic_write_json(self.paths.block_path(block.index), block.to_dict())
        atomic_write_json(self.paths.state_path(block.index), state.to_dict())

    def _persist_receipts(self, height, receipts):
        """Persist execution receipts for a height (basis of history/event API).

        Only consensus-relevant fields are stored.  Receipts let historical
        event logs be answered from committed data instead of the per-contract
        convenience files, which are not rebuilt on reorgs.
        """
        data = [
            {
                "txid": r.get("txid"),
                "ok": bool(r.get("ok")),
                "type": r.get("type"),
                "contract": r.get("contract"),
                "events": r.get("events") or [],
                "return": r.get("return"),
                "error": r.get("error"),
                "coinbase": r.get("coinbase", False),
            }
            for r in receipts
        ]
        atomic_write_json(self.paths.receipt_path(height), data)
        self._receipts_cache.pop(height, None)

    def _delete_block_files_above(self, height):
        for sub in (self.paths.blocks_dir, self.paths.receipts_dir):
            for f in os.listdir(sub):
                try:
                    h = int(f.split(".")[0])
                except ValueError:
                    continue
                if h > height:
                    os.remove(os.path.join(sub, f))
        for f in os.listdir(self.paths.state_dir):
            try:
                h = int(f.split(".")[0])
            except ValueError:
                continue
            if h > height:
                os.remove(os.path.join(self.paths.state_dir, f))

    # ==================================================================== #
    # Transaction execution
    # ==================================================================== #
    def _execute_transaction(self, tx, state, miner, height):
        """Apply a single non-coinbase transaction; return ``(ok, receipt)``.

        The state is snapshotted first so a reverting contract call undoes its
        own effects.  Fee and nonce are applied regardless of outcome (the
        attempt still consumed resources).
        """
        before = state.copy()
        receipt = {"txid": tx.txid, "ok": True, "error": None, "events": [],
                   "return": None, "transfers": [], "type": tx.tx_type,
                   "contract": None}
        try:
            if tx.tx_type == "transfer":
                if state.balance(tx.sender) < tx.amount + tx.fee:
                    raise ChainValidationError("insufficient balance")
                state.add_balance(tx.sender, -tx.amount)
                state.add_balance(tx.to, tx.amount)
            elif tx.tx_type == "deploy":
                if state.balance(tx.sender) < tx.fee:
                    raise ChainValidationError("insufficient balance for deploy")
                address = self._contract_address(tx)
                result = self.engine.deploy(
                    tx.data.get("code", ""), tx.sender, address, state,
                    constructor=tx.data.get("constructor"), height=height)
                if not result["ok"]:
                    raise ChainValidationError(result["error"] or "deploy failed")
                receipt["events"] = result["events"]
                receipt["contract"] = address
            elif tx.tx_type == "call":
                if state.balance(tx.sender) < tx.amount + tx.fee:
                    raise ChainValidationError("insufficient balance for call")
                # Credit the contract with the attached value before invoking,
                # so msg.value / this_balance / transfer see it.
                state.add_balance(tx.sender, -tx.amount)
                state.add_balance(tx.to, tx.amount)
                result = self.engine.invoke(
                    tx.to, tx.data.get("function"), tx.data.get("args", []),
                    tx.sender, tx.amount, state, height)
                if not result["ok"]:
                    raise ChainValidationError(result["error"] or "call failed")
                receipt["events"] = result["events"]
                receipt["return"] = result["return"]
                receipt["contract"] = tx.to
            else:
                raise ChainValidationError(f"unknown type {tx.tx_type}")
        except Exception as e:  # noqa: BLE001 - revert semantics
            state = before
            receipt["ok"] = False
            receipt["error"] = str(e)

        # Fee + nonce bookkeeping is applied even when the call reverted.
        if state.balance(tx.sender) >= tx.fee:
            state.add_balance(tx.sender, -tx.fee)
            state.add_balance(miner, tx.fee)
        state.increment_nonce(tx.sender)
        return state, receipt

    def _contract_address(self, tx):
        """Derive a deterministic contract address from the deploy tx."""
        return "0xc" + crypto.sha256(tx.txid.encode()).hex()[:40]

    def apply_block(self, block, state):
        """Apply all transactions of ``block`` to ``state``; return receipts."""
        miner = ZERO_ADDRESS
        receipts = []
        for tx in block.transactions:
            if tx.is_coinbase():
                miner = tx.to
                state.add_balance(tx.to, tx.amount)
                receipts.append({"txid": tx.txid, "ok": True, "coinbase": True})
                continue
            state, receipt = self._execute_transaction(
                tx, state, miner or ZERO_ADDRESS, block.index)
            receipts.append(receipt)
        return state, receipts

    # ==================================================================== #
    # Block validation
    # ==================================================================== #
    def validate_block(self, block, prev_block):
        """Full validation of ``block`` against ``prev_block``."""
        if block.index != prev_block.index + 1:
            return False, "block index is not prev+1"
        if block.prev_hash != prev_block.hash:
            return False, "prev_hash does not match parent"
        ok, reason = block.validate_structure()
        if not ok:
            return False, reason
        if block.difficulty != pow_mod.next_difficulty(self, block):
            return False, "difficulty does not match schedule"
        # Verify transaction signatures and sender authenticity.
        for tx in block.transactions:
            if tx.is_coinbase():
                if tx.amount != COINBASE_REWARD:
                    return False, "coinbase reward mismatch"
                continue
            if not tx.validate_signature():
                return False, f"invalid signature on tx {tx.txid}"
            if tx.derived_sender() != tx.sender:
                return False, f"sender mismatch on tx {tx.txid}"
        return True, "ok"

    # ==================================================================== #
    # Consensus: adding blocks, fork handling, reorg
    # ==================================================================== #
    def add_block(self, block):
        """Add a validated block, resolving any fork it creates.

        Returns ``(status, message)`` where status is one of
        ``extended``, ``duplicate``, ``stored_fork``, ``reorg``, ``invalid``.
        """
        # Duplicate protection.
        if self.has_block(block.hash):
            return "duplicate", "block already known"

        if block.index == self.height + 1:
            prev = self.get_block(block.index - 1)
            if prev is None or block.prev_hash != prev.hash:
                # Might extend a stored fork.
                return self._maybe_extend_fork(block)
            ok, reason = self.validate_block(block, prev)
            if not ok:
                return "invalid", reason
            return self._extend(block)

        if block.index <= self.height:
            return self._maybe_extend_fork(block)

        # block.index > height + 1: we are missing ancestors -> request sync.
        return "missing", "missing ancestor blocks (need sync)"

    def _apply_and_verify(self, block, state):
        """Apply ``block`` to ``state``, verifying the committed state root."""
        new_state, receipts = self.apply_block(block, state)
        if new_state.root() != block.header.state_root:
            raise ChainValidationError(
                "state root mismatch after applying block (non-deterministic "
                "or malformed block)")
        return new_state, receipts

    def _extend(self, block):
        try:
            new_state, receipts = self._apply_and_verify(block, self.state.copy())
        except ChainValidationError as e:
            return "invalid", str(e)
        self.chain.append(block)
        self.state = new_state
        self.chainwork += int(2 ** block.difficulty)
        self.last_receipts = receipts
        self._persist_block(block, new_state)
        self._persist_receipts(block.index, receipts)
        self._write_meta()
        self.versions.record(block.index, block.hash)
        return "extended", "chain extended"

    def _maybe_extend_fork(self, block):
        """Store a competing block, and reorg if it makes a heavier branch."""
        if block.index == self.height and block.prev_hash != self.head.prev_hash:
            # A sibling of our head (or an ancestor fork we still track).
            pass
        self.fork_store.setdefault(block.index, []).append(block)

        # Try to trace a branch from this block back to the main chain.
        branch = self._trace_branch(block)
        if branch is None:
            return "stored_fork", "competing block stored (branch incomplete)"
        branch_work = self.cumulative_work_of(branch)
        main_work_at_ancestor = self.cumulative_work_of(
            self.chain[:branch[0].index + 1]) if branch else 0
        if branch_work + main_work_at_ancestor > self.chainwork:
            return self._reorg(branch)
        return "stored_fork", "competing block stored (weaker branch)"

    def _trace_branch(self, tip):
        """Walk fork_store back to a block whose parent is on the main chain."""
        by_hash = {b.hash: b for blocks in self.fork_store.values()
                   for b in blocks}
        branch = []
        current = tip
        seen = set()
        while current is not None and current.hash not in seen:
            seen.add(current.hash)
            branch.append(current)
            if current.index == 0:
                break
            # Parent on the main chain?
            main_parent = self.get_block(current.index - 1)
            if main_parent is not None and main_parent.hash == current.prev_hash:
                branch.reverse()
                return branch
            current = by_hash.get(current.prev_hash)
        return None

    def _reorg(self, branch):
        """Switch the main chain onto a heavier fork ``branch``.

        ``branch`` is a list of blocks whose first element's parent is on the
        current main chain.  Blocks after the common ancestor are rolled back
        (their state is discarded and transactions re-admitted by the caller)
        and the branch is applied in order.
        """
        ancestor_height = branch[0].index - 1
        abandoned = self.chain[ancestor_height + 1:]

        # Roll back state to the ancestor snapshot.
        ancestor_state_data = read_json(self.paths.state_path(ancestor_height))
        state = WorldState.from_dict(ancestor_state_data)
        self.chain = self.chain[:ancestor_height + 1]
        self.chainwork = self.cumulative_work_of(self.chain)
        self.last_abandoned = list(abandoned)  # for tx re-admission by the node
        self._creation_height_cache.clear()
        self._receipts_cache.clear()

        applied = 0
        all_receipts = []
        for blk in branch:
            state, receipts = self.apply_block(blk, state)
            all_receipts.extend(receipts)
            if state.root() != blk.header.state_root:
                # The winning branch must itself be consistent; if not, keep
                # the current chain and flag the reorg as failed.
                return "invalid", "fork branch failed state-root verification"
            self.chain.append(blk)
            self.chainwork += int(2 ** blk.difficulty)
            self._persist_block(blk, state)
            self._persist_receipts(blk.index, receipts)
            applied += 1

        self.state = state
        self.last_receipts = all_receipts
        self._write_meta()
        self.versions.record(self.height, self.head.hash)
        # Clean fork_store of now-main blocks.
        for b in branch:
            self.fork_store.pop(b.index, None)
        return "reorg", (f"reorg at height {ancestor_height}: "
                         f"rolled back {len(abandoned)} block(s), "
                         f"applied {applied} block(s)")

    def rollback(self, target_height):
        """Roll the chain back to ``target_height`` (admin operation)."""
        if target_height < 0 or target_height >= self.height:
            return False, "invalid target height"
        if not os.path.exists(self.paths.state_path(target_height)):
            return False, "no state snapshot at target height"
        state_data = read_json(self.paths.state_path(target_height))
        self.state = WorldState.from_dict(state_data)
        self.chain = self.chain[:target_height + 1]
        self.chainwork = self.cumulative_work_of(self.chain)
        self._creation_height_cache.clear()
        self._receipts_cache.clear()
        self._delete_block_files_above(target_height)
        self._write_meta()
        self.versions.record(target_height, self.head.hash)
        return True, f"rolled back to height {target_height}"

    # ==================================================================== #
    # Historical queries (read-only; never touch the live state)
    # ==================================================================== #
    def state_at(self, height):
        """Return a *fresh copy* of the committed world state at ``height``.

        Loaded from the per-height snapshot on disk, so historical reads never
        alias or mutate :attr:`state` (the live state used for reads/writes).
        """
        if not (0 <= height <= self.height):
            return None
        data = read_json(self.paths.state_path(height))
        return WorldState.from_dict(data) if data else None

    def receipts_at(self, height):
        """Committed tx receipts at ``height``; deterministically replay if missing.

        Nodes started before receipts were persisted may lack the file.  The
        block is then re-executed against the parent state snapshot on a
        throwaway copy of state, and the derived receipts are cached to disk.
        Resulting state must match the committed state root.
        """
        if not (0 <= height <= self.height):
            return None
        cached = self._receipts_cache.get(height)
        if cached is not None:
            return cached
        data = read_json(self.paths.receipt_path(height))
        if data is None:
            block = self.get_block(height)
            if height == 0:
                data = []
            else:
                parent = self.state_at(height - 1)
                _state, data = self.apply_block(block, parent)
                if _state.root() != block.header.state_root:
                    raise ChainValidationError(
                        f"state root mismatch while replaying height {height}")
            try:
                atomic_write_json(self.paths.receipt_path(height), data)
            except OSError:
                pass  # read-only replica; caching is best-effort
        self._receipts_cache[height] = data
        return data

    def contract_creation_height(self, address):
        """Height at which ``address`` was successfully deployed, or ``None``.

        A matching deploy transaction is confirmed by the committed state
        snapshot of its block containing the contract (a reverted deploy is
        absent).  Stale deploys from abandoned fork blocks are never
        considered because only the main chain is scanned.
        """
        if address in self._creation_height_cache:
            return self._creation_height_cache[address]
        found = None
        for blk in self.chain:
            if not any(tx.tx_type == "deploy"
                       and self._contract_address(tx) == address
                       for tx in blk.transactions):
                continue
            snapshot = self.state_at(blk.index)
            if snapshot is not None and snapshot.contract(address) is not None:
                found = blk.index
                break
        self._creation_height_cache[address] = found
        return found

    def contract_events_until(self, address, height):
        """All successful events emitted by ``address`` in blocks 0..height.

        Events are taken from main-chain receipts, ordered oldest-first.
        """
        if not (0 <= height <= self.height):
            return None
        events = []
        for h in range(0, height + 1):
            for r in self.receipts_at(h) or []:
                if r.get("contract") != address or not r.get("ok"):
                    continue
                for e in r.get("events") or []:
                    events.append({
                        "height": h,
                        "txid": r.get("txid"),
                        "event": e.get("event"),
                        "data": e.get("data"),
                    })
        return events

    @staticmethod
    def _storage_diff(a_storage, b_storage):
        """Per-key differences of storage at two heights (b relative to a)."""
        added, removed, changed = [], [], []
        for k in b_storage:
            if k not in a_storage:
                added.append(k)
            elif a_storage[k] != b_storage[k]:
                changed.append(k)
        for k in a_storage:
            if k not in b_storage:
                removed.append(k)
        return {
            "added": {k: b_storage[k] for k in sorted(added)},
            "removed": {k: a_storage[k] for k in sorted(removed)},
            "changed": {k: {"from": a_storage[k], "to": b_storage[k]}
                        for k in sorted(changed)},
            "unchanged_keys": sorted(
                k for k in a_storage if k in b_storage
                and a_storage[k] == b_storage[k]),
        }

    def contract_history(self, address, height):
        """Read-only historical snapshot of a contract at ``height``.

        Returns ``(payload, error)`` where ``error`` is one of the sentinel
        strings ``future`` / ``negative`` / ``missing`` / ``before_creation``.
        """
        if height < 0:
            return None, "negative"
        if height > self.height:
            return None, "future"
        # The scan is cached per contract and only walks the canonical chain,
        # so a deploy buried in an abandoned fork never counts as creation.
        creation = self.contract_creation_height(address)
        if creation is None:
            return None, "missing"
        if height < creation:
            return {
                "address": address,
                "height": height,
                "current_height": self.height,
                "created_at": creation,
                "existed": False,
            }, "before_creation"
        snapshot = self.state_at(height)
        c = snapshot.contract(address)
        block = self.get_block(height)
        return {
            "address": address,
            "height": height,
            "current_height": self.height,
            "created_at": creation,
            "existed": True,
            "creator": c.get("creator"),
            "code": c.get("code"),
            "storage": c.get("storage", {}),
            "balance": snapshot.balance(address),
            "state_root": block.state_root if block else None,
            "block_hash": block.hash if block else None,
            "block_timestamp": block.timestamp if block else None,
            "events": self.contract_events_until(address, height),
        }, None

    def contract_diff(self, address, height_a, height_b):
        """Compare a contract's state at two historical heights (read-only).

        Returns ``(payload, error)`` with the same sentinels as
        :meth:`contract_history`; both heights must be within
        ``[creation, current]``.
        """
        lo, hi = sorted((height_a, height_b))
        if lo < 0:
            return None, "negative"
        if hi > self.height:
            return None, "future"
        creation = self.contract_creation_height(address)
        if creation is None:
            return None, "missing"
        if lo < creation:
            return None, "before_creation"

        state_a = self.state_at(lo)
        state_b = self.state_at(hi)
        ca, cb = state_a.contract(address), state_b.contract(address)
        block_a, block_b = self.get_block(lo), self.get_block(hi)
        return {
            "address": address,
            "current_height": self.height,
            "created_at": creation,
            "a": {
                "height": lo,
                "storage": ca.get("storage", {}),
                "balance": state_a.balance(address),
                "state_root": block_a.state_root,
                "block_hash": block_a.hash,
                "block_timestamp": block_a.timestamp,
            },
            "b": {
                "height": hi,
                "storage": cb.get("storage", {}),
                "balance": state_b.balance(address),
                "state_root": block_b.state_root,
                "block_hash": block_b.hash,
                "block_timestamp": block_b.timestamp,
            },
            "storage_diff": self._storage_diff(ca.get("storage", {}),
                                               cb.get("storage", {})),
            "balance_delta": state_b.balance(address) - state_a.balance(address),
            "events_between": self.contract_events_between(address, lo, hi),
        }, None

    def contract_events_between(self, address, lo, hi):
        """Successful events for ``address`` in blocks ``lo+1 .. hi``.

        Events emitted in ``lo`` itself belong to the "before" snapshot and are
        excluded from the between-set.
        """
        events = []
        for h in range(lo + 1, hi + 1):
            for r in self.receipts_at(h) or []:
                if r.get("contract") != address or not r.get("ok"):
                    continue
                for e in r.get("events") or []:
                    events.append({
                        "height": h, "txid": r.get("txid"),
                        "event": e.get("event"), "data": e.get("data"),
                    })
        return events

    # ==================================================================== #
    # Dashboard / display aggregation helpers
    # ==================================================================== #
    def tx_type_counts(self):
        counts = {}
        for blk in self.chain:
            for tx in blk.transactions:
                cat = tx.stats_category()
                counts[cat] = counts.get(cat, 0) + 1
        return counts

    def avg_block_interval(self, window=20):
        blocks = self.chain
        if len(blocks) < 2:
            return 0.0
        start = max(1, len(blocks) - window)
        intervals = [blocks[i].elapsed_since(blocks[i - 1])
                     for i in range(start, len(blocks))]
        return sum(intervals) / len(intervals) if intervals else 0.0

    def difficulty_series(self):
        return pow_mod.difficulty_series(self)

    def top_accounts(self, limit=10):
        return self.state.top_accounts(limit=limit)

    def balance_for_display(self, address):
        return self.state.display_balance(address)

    def chain_summary(self):
        return [
            {
                "index": b.index, "hash": b.hash, "prev_hash": b.prev_hash,
                "timestamp": b.timestamp, "difficulty": b.difficulty,
                "tx_count": b.display_tx_count(), "nonce": b.nonce,
            }
            for b in self.chain
        ]

    def block_summary(self, block):
        return {
            "index": block.index, "hash": block.hash,
            "prev_hash": block.prev_hash, "timestamp": block.timestamp,
            "difficulty": block.difficulty,
            "tx_count": block.display_tx_count(), "nonce": block.nonce,
            "merkle_root": block.header.merkle_root,
            "state_root": block.state_root,
        }

    def transactions_for(self, address, limit=200):
        txs = []
        for blk in self.chain:
            for tx in blk.transactions:
                if tx.involves(address):
                    txs.append({
                        "txid": tx.txid, "type": tx.tx_type, "from": tx.sender,
                        "to": tx.to, "amount": tx.amount, "fee": tx.fee,
                        "nonce": tx.nonce, "height": blk.index,
                        "timestamp": tx.timestamp,
                    })
        txs.reverse()
        return txs[:limit]

    # ==================================================================== #
    # Tamper detection / full validation
    # ==================================================================== #
    def validate_full_chain(self):
        """Re-verify every block and the stored state; report tampering."""
        report = {"valid": True, "checked": 0, "errors": []}
        if not self.chain:
            report["errors"].append("empty chain")
            report["valid"] = False
            return report

        prev = None
        for blk in self.chain:
            report["checked"] += 1
            blk.recompute_hash()
            if prev is not None:
                if blk.prev_hash != prev.hash:
                    report["errors"].append(
                        f"height {blk.index}: prev_hash mismatch")
                ok, reason = self.validate_block(blk, prev)
                if not ok:
                    report["errors"].append(
                        f"height {blk.index}: {reason}")
            # Compare stored state snapshot to the block's committed root.
            snap = read_json(self.paths.state_path(blk.index))
            if snap is None:
                report["errors"].append(
                    f"height {blk.index}: missing state snapshot")
            else:
                stored_state = WorldState.from_dict(snap)
                if stored_state.root() != blk.header.state_root:
                    report["errors"].append(
                        f"height {blk.index}: state root mismatch "
                        f"(state tampered?)")
            # Compare on-disk file hash to in-memory recomputed hash.
            disk = read_json(self.paths.block_path(blk.index))
            if disk is not None:
                disk_blk = Block.from_dict(disk)
                disk_blk.recompute_hash()
                if disk_blk.hash != blk.hash:
                    report["errors"].append(
                        f"height {blk.index}: on-disk block hash mismatch "
                        f"(block tampered?)")
            prev = blk
        report["valid"] = not report["errors"]
        return report
