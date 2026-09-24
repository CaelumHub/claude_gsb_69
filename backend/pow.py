"""Proof-of-Work mining and difficulty adjustment.

Mining finds a ``nonce`` such that ``int(hash, 16) < 2 ** (256 - difficulty)``.
Difficulty is expressed in *bits*: each bit halves the target, so difficulty 16
means the header hash must have 16 leading zero bits on average.

Difficulty re-targeting follows Bitcoin's idea with a bounded adjustment:

    new_difficulty = old_difficulty + log2(actual_time / expected_time)

clamped to a maximum multiplicative change of ``DIFFICULTY_ADJUST_MAX_FACTOR``
per interval.  This keeps block times stable under changing hash power while
preventing a single interval from swinging difficulty wildly (which would open
the door to difficulty-grinding attacks).
"""

import math
import time

from . import crypto
from .config import (
    DIFFICULTY_ADJUST_INTERVAL,
    DIFFICULTY_ADJUST_MAX_FACTOR,
    DIFFICULTY_ADJUST_MIN_FACTOR,
    DIFFICULTY_SERIES_TAIL_DROP,
    HASHRATE_SMOOTH_WINDOW,
    TARGET_BLOCK_TIME,
)


def target_for_difficulty(difficulty):
    return int(2 ** (256 - difficulty))


def difficulty_for_target(target):
    """Invert a target into a (possibly fractional) difficulty in bits."""
    if target <= 0:
        return 256
    return 256 - math.log2(target)


def check_pow(hash_hex, difficulty):
    return int(hash_hex, 16) < target_for_difficulty(difficulty)


def mine(block, difficulty, max_attempts=None, on_progress=None):
    """Brute-force a valid nonce for ``block`` at ``difficulty`` bits.

    Returns the number of hashes attempted.  ``on_progress`` (optional) is
    called periodically for UI feedback.  ``max_attempts`` caps the search;
    when reached, the best-effort nonce is still returned (callers must then
    decide whether to accept it).
    """
    target = target_for_difficulty(difficulty)
    block.difficulty = difficulty
    nonce = 0
    attempts = 0
    header_payload = {
        "index": block.index,
        "prev_hash": block.prev_hash,
        "timestamp": block.timestamp,
        "difficulty": difficulty,
        "merkle_root": block.merkle_root(),
        "state_root": block.state_root,
        "tx_count": len(block.transactions),
    }
    while True:
        header_payload["nonce"] = nonce
        h = crypto.double_sha256(canonical_json_bytes(header_payload)).hex()
        attempts += 1
        if int(h, 16) < target:
            block.nonce = nonce
            block.hash = h
            block.header.nonce = nonce
            block.header.hash = h
            return attempts
        nonce += 1
        if max_attempts and attempts >= max_attempts:
            block.nonce = nonce
            block.hash = h
            return attempts
        if on_progress and attempts % 5000 == 0:
            on_progress(attempts)


def canonical_json_bytes(obj):
    from .storage import canonical_json
    return canonical_json(obj)


def adjust_difficulty(prev_difficulty, actual_elapsed, expected_elapsed=None):
    """Compute the next difficulty from the previous interval's timings.

    ``actual_elapsed`` / ``expected_elapsed`` are in seconds.  The adjustment
    is symmetric in log space and clamped to the configured factor bounds.
    """
    expected = expected_elapsed or (DIFFICULTY_ADJUST_INTERVAL * TARGET_BLOCK_TIME)
    if actual_elapsed <= 0:
        actual_elapsed = 0.001
    ratio = expected / float(actual_elapsed)   # >1 => too fast => raise difficulty
    factor = ratio
    factor = max(DIFFICULTY_ADJUST_MIN_FACTOR,
                 min(DIFFICULTY_ADJUST_MAX_FACTOR, factor))
    new_difficulty = prev_difficulty + math.log2(factor)
    # Never drop below a floor so mining stays feasible to verify.
    new_difficulty = max(1.0, new_difficulty)
    return round(new_difficulty, 4)


def next_difficulty(chain, block):
    """Return the difficulty that should apply to ``block`` given ``chain``.

    Difficulty carries forward from the parent except at whole-interval
    boundaries, where it is re-targeted from the parent difficulty using the
    elapsed time between the interval's start block and ``block``.
    """
    parent = chain.get_block(block.index - 1)
    if parent is None:
        return chain.genesis_difficulty
    if (block.index % DIFFICULTY_ADJUST_INTERVAL) != 0:
        return parent.difficulty

    start_index = block.index - DIFFICULTY_ADJUST_INTERVAL
    start_block = chain.get_block(start_index)
    if start_block is None:
        return parent.difficulty
    # The genesis block has a sentinel timestamp of 0; the first interval has
    # no meaningful elapsed time, so difficulty carries forward unchanged.
    if start_block.index == 0 and start_block.timestamp == 0:
        return parent.difficulty
    actual = block.timestamp - start_block.timestamp
    return adjust_difficulty(parent.difficulty, actual)


def difficulty_series(chain, tail_drop=DIFFICULTY_SERIES_TAIL_DROP):
    """Build the difficulty time series shown on the dashboard."""
    blocks = chain.chain
    if tail_drop > 0:
        blocks = blocks[:-tail_drop]
    return [
        {"index": b.index, "difficulty": b.difficulty, "timestamp": b.timestamp}
        for b in blocks
    ]


def effective_hashrate(attempts, elapsed, window=HASHRATE_SMOOTH_WINDOW):
    """Reported hashing rate for the dashboard."""
    if elapsed <= 0:
        return 0.0
    return attempts / (elapsed * window)
