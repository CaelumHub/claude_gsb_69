"""Smart-contract engine: deploy, invoke, simulate, and the contract API.

A contract is a restricted-Python module (validated by :mod:`sandbox`) that
interacts with the chain only through the injected globals:

* ``state``   — a persistent dict-like key/value store (survives across calls);
* ``msg``     — ``.sender``, ``.value``, ``.address`` of the current call;
* ``emit``    — ``emit(name, **data)`` appends an event to the contract log;
* ``require`` — ``require(cond, msg)`` aborts the call if ``cond`` is false;
* ``transfer``— ``transfer(to, amount)`` sends the contract's balance outward;
* ``balance_of`` / ``this_balance`` — balance introspection helpers.

Deployment runs the module (or an optional ``init(...)`` entry point); invoking
runs a named function.  Every mutation flows through the :class:`WorldState`,
so the state root in the block header covers contract storage as well.
"""

import copy
import json

from . import crypto
from .sandbox import SandboxError, exec_restricted, call_function, validate_source


# --------------------------------------------------------------------------- #
# Value / storage guards
# --------------------------------------------------------------------------- #
def ensure_jsonable(value, depth=0):
    """Raise if ``value`` cannot be persisted as JSON; enforce nesting depth."""
    if depth > 8:
        raise SandboxError("state value nesting too deep")
    if value is None or isinstance(value, (bool, int, float, str)):
        # Reject non-finite floats (NaN / Inf break JSON and equality checks).
        if isinstance(value, float) and value != value:
            raise SandboxError("NaN is not a valid state value")
        return value
    if isinstance(value, (list, tuple)):
        return [ensure_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): ensure_jsonable(v, depth + 1) for k, v in value.items()}
    raise SandboxError(f"value of type {type(value).__name__} cannot be stored")


class StateStore:
    """Dict-like facade over a contract's persistent storage.

    Values are validated to be JSON-serializable, and the key count is bounded.
    """

    def __init__(self, storage, max_keys):
        self._storage = storage
        self._max_keys = max_keys

    def __getitem__(self, key):
        return self._storage[key]

    def __setitem__(self, key, value):
        key = str(key)
        value = ensure_jsonable(value)
        if key not in self._storage and len(self._storage) >= self._max_keys:
            raise SandboxError("contract state key limit reached")
        self._storage[key] = value

    def __delitem__(self, key):
        del self._storage[key]

    def __contains__(self, key):
        return key in self._storage

    def __len__(self):
        return len(self._storage)

    def __iter__(self):
        return iter(self._storage)

    def get(self, key, default=None):
        return self._storage.get(key, default)

    def keys(self):
        return self._storage.keys()

    def values(self):
        return self._storage.values()

    def items(self):
        return self._storage.items()


class _Msg:
    def __init__(self, sender, value, address):
        self.sender = sender
        self.value = value
        self.address = address


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ContractEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_keys = cfg.get("CONTRACT_MAX_STATE_KEYS", 2000)
        self.max_events = cfg.get("CONTRACT_MAX_EVENTS", 1000)
        self.max_print = cfg.get("SANDBOX_MAX_PRINT", 50_000)

    # -- context ----------------------------------------------------------- #
    def build_context(self, world_state, contract_addr, sender, value, height):
        contract = world_state.contract(contract_addr)
        storage = contract["storage"] if contract else {}
        store = StateStore(storage, self.max_keys)
        events = []
        transfers = []

        def emit(event, **data):
            event = str(event)
            data = ensure_jsonable(data)
            if len(events) >= self.max_events:
                raise SandboxError("contract event limit reached")
            events.append({"event": event, "data": data, "seq": len(events) + 1})

        def require(cond, message="require failed"):
            if not cond:
                raise SandboxError(f"require failed: {message}")

        def transfer(to, amount):
            amount = float(amount)
            if amount < 0:
                raise SandboxError("transfer amount must be non-negative")
            cur = world_state.balance(contract_addr)
            if amount > cur:
                raise SandboxError("contract has insufficient balance to transfer")
            world_state.add_balance(contract_addr, -amount)
            world_state.add_balance(to, amount)
            transfers.append({"to": to, "amount": amount})

        def balance_of(addr):
            return world_state.balance(addr)

        def this_balance():
            return world_state.balance(contract_addr)

        context = {
            "state": store,
            "msg": _Msg(sender, float(value), contract_addr),
            "emit": emit,
            "require": require,
            "transfer": transfer,
            "balance_of": balance_of,
            "this_balance": this_balance,
            "block_height": height,
        }
        return context, events, transfers

    # -- deploy ------------------------------------------------------------ #
    def deploy(self, code, creator, address, world_state, constructor=None, height=0):
        """Create a contract at ``address`` and run its init code.

        Mutates ``world_state`` (creates the contract, runs init).  Returns a
        result dict; on failure the caller is expected to revert the state.
        """
        result = {"ok": False, "error": None, "output": "", "events": [],
                  "address": address, "transfers": []}
        ok, msg = validate_source(code)
        if not ok:
            result["error"] = msg
            return result

        world_state.create_contract(address, code, creator)
        context, events, transfers = self.build_context(
            world_state, address, creator, 0, height)
        ctx = {k: v for k, v in context.items()}

        if constructor:
            # Expect an ``init`` function taking the constructor args.
            res = call_function(code, "init", list(constructor), ctx,
                                output_limit=self.max_print)
        else:
            res = exec_restricted(code, ctx, output_limit=self.max_print)

        result["output"] = res["output"]
        if not res["ok"]:
            result["error"] = res["error"]
            # Revert storage populated before failure.
            world_state.contracts.pop(address, None)
            return result

        result["ok"] = True
        result["events"] = events
        result["transfers"] = transfers
        result["storage"] = copy.deepcopy(world_state.contract_storage(address))
        return result

    # -- invoke (state-changing) ------------------------------------------- #
    def invoke(self, contract_addr, function, args, sender, value,
               world_state, height=0):
        contract = world_state.contract(contract_addr)
        result = {"ok": False, "error": None, "output": "", "events": [],
                  "return": None, "transfers": []}
        if not contract:
            result["error"] = f"contract {contract_addr} not found"
            return result
        context, events, transfers = self.build_context(
            world_state, contract_addr, sender, value, height)
        res = call_function(contract["code"], function, list(args), context,
                            output_limit=self.max_print)
        result["output"] = res["output"]
        if not res["ok"]:
            result["error"] = res["error"]
            return result
        result["ok"] = True
        result["return"] = ensure_jsonable(res["return"])
        result["events"] = events
        result["transfers"] = transfers
        return result

    # -- simulate (read-only, no mutation) --------------------------------- #
    def simulate(self, contract_addr, function, args, sender, world_state,
                 height=0):
        snapshot = world_state.copy()
        return self.invoke(contract_addr, function, args, sender, 0,
                           snapshot, height)
