"""HTTP API server (Flask) exposing the node, chain, pool, wallet, and contracts.

Serves the frontend pages and the JSON REST API on the same origin, plus a
``/p2p/*`` surface used for node-to-node synchronisation.  The API is the only
way the UI touches the node, keeping the consensus core free of web concerns.
"""

import os
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from . import crypto
from .state import ZERO_ADDRESS
from .storage import read_json, atomic_write_json
from .transaction import Transaction
from .templates import template_catalog, get_template, TEMPLATES

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "frontend")
PAGES = ["index", "wallet", "txpool", "explorer", "deploy", "interact",
         "nodes", "network", "stats", "admin", "templates"]


def _json(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def create_app(node):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    CORS(app)

    # ------------------------------------------------------------------ #
    # Frontend pages
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.route("/<page>.html")
    def page(page):
        if page in PAGES:
            return send_from_directory(FRONTEND_DIR, f"{page}.html")
        return "not found", 404

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(FRONTEND_DIR, filename)

    # ================================================================== #
    # Node info / status
    # ================================================================== #
    @app.get("/api/node/info")
    def node_info():
        return _json({
            "id": node.node_id, "port": node.port, "host": node.host,
            "version": "1.0.0", "uptime": time.time() - node.started_at,
            "mining": node.mining, "peers": len(node.peers.all()),
        })

    @app.get("/api/node/status")
    def node_status():
        bc = node.blockchain
        return _json({
            "id": node.node_id, "port": node.port,
            "height": bc.height, "head_hash": bc.head.hash if bc.head else None,
            "chainwork": bc.chainwork,
            "difficulty": bc.head.difficulty if bc.head else bc.genesis_difficulty,
            "txpool_size": node.txpool.size(),
            "mining": node.mining, "hashrate": round(node.reported_hashrate(), 1),
            "peers": len(node.peers.all()),
            "state_root": bc.state.root() if bc.head else None,
            "accounts": len(bc.state.accounts),
            "contracts": len(bc.state.contracts),
        })

    # ================================================================== #
    # Network
    # ================================================================== #
    @app.get("/api/network/peers")
    def peers_list():
        return _json({"peers": [p.to_dict() for p in node.peers.all()]})

    @app.post("/api/network/add_peer")
    def add_peer():
        data = request.get_json(force=True, silent=True) or {}
        host = data.get("host", "127.0.0.1")
        try:
            port = int(data.get("port", 8000))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid port"}, 400)
        if host in ("127.0.0.1", "localhost") and port == node.port:
            return _json({"ok": False, "error": "cannot peer with self"}, 400)
        peer = node.peers.add(data.get("id"), host, port)
        return _json({"ok": True, "peer": peer.to_dict()})

    @app.post("/api/network/remove_peer")
    def remove_peer():
        data = request.get_json(force=True, silent=True) or {}
        ok = node.peers.remove(data.get("host", "127.0.0.1"),
                               int(data.get("port", 0)))
        return _json({"ok": ok})

    @app.get("/api/network/config")
    def network_config():
        return _json({
            "node_id": node.node_id, "host": node.host, "port": node.port,
            "data_dir": node.paths.root,
            "mining": node.mining,
            "mining_interval": node.cfg.get("mining_interval", 3.0),
            "initial_difficulty": node.blockchain.genesis_difficulty,
            "coinbase_reward": node.cfg.get("COINBASE_REWARD", 50.0),
            "max_tx_per_block": node.cfg.get("MAX_TX_PER_BLOCK", 200),
            "sandbox_timeout": node.cfg.get("SANDBOX_TIMEOUT", 3.0),
            "peers": [p.to_dict() for p in node.peers.all()],
        })

    @app.post("/api/network/config")
    def update_network_config():
        data = request.get_json(force=True, silent=True) or {}
        changed = []
        if "mining_interval" in data:
            node.cfg["mining_interval"] = float(data["mining_interval"])
            changed.append("mining_interval")
        if "auto_mine" in data:
            if data["auto_mine"] and not node.mining:
                node.start_mining()
            elif not data["auto_mine"] and node.mining:
                node.stop_mining()
            changed.append("auto_mine")
        return _json({"ok": True, "changed": changed})

    @app.post("/api/network/sync")
    def trigger_sync():
        summary = node.sync_with_peers()
        return _json({"ok": True, "summary": summary})

    # ================================================================== #
    # Wallet
    # ================================================================== #
    @app.post("/api/wallet/create")
    def wallet_create():
        data = request.get_json(force=True, silent=True) or {}
        addr, w = node.wallets.create(data.get("label"))
        return _json({"ok": True, "address": addr,
                      "public_key": w["public_key"], "label": w["label"]})

    @app.post("/api/wallet/import")
    def wallet_import():
        data = request.get_json(force=True, silent=True) or {}
        addr, err = node.wallets.import_private_key(data.get("private_key", ""),
                                                    data.get("label"))
        if err:
            return _json({"ok": False, "error": err}, 400)
        return _json({"ok": True, "address": addr})

    @app.get("/api/wallet/list")
    def wallet_list():
        result = []
        for w in node.wallets.list():
            addr = w["address"]
            result.append({
                "address": addr,
                "label": w["label"],
                "public_key": w["public_key"],
                "created": w["created"],
                "balance": node.blockchain.balance_for_display(addr),
                "nonce": node.blockchain.state.nonce(addr),
            })
        return _json({"wallets": result})

    @app.get("/api/wallet/<addr>")
    def wallet_detail(addr):
        st = node.blockchain.state
        return _json({
            "address": addr,
            "balance": node.blockchain.balance_for_display(addr),
            "nonce": st.nonce(addr),
            "local": node.wallets.has(addr),
        })

    @app.get("/api/wallet/<addr>/export")
    def wallet_export(addr):
        key = node.wallets.export_private_key(addr)
        if not key:
            return _json({"ok": False, "error": "wallet not local"}, 404)
        return _json({"ok": True, "address": addr, "private_key": key})

    @app.post("/api/wallet/sign")
    def wallet_sign():
        data = request.get_json(force=True, silent=True) or {}
        sig, err = node.wallets.sign_message(data.get("address", ""),
                                             data.get("message", ""))
        if err:
            return _json({"ok": False, "error": err}, 400)
        return _json({"ok": True, "signature": sig,
                      "message_hash": crypto.sha256_hex(data.get("message", ""))})

    @app.get("/api/wallet/<addr>/transactions")
    def wallet_transactions(addr):
        return _json({"transactions": node.blockchain.transactions_for(addr)})

    # ================================================================== #
    # Transactions / pool
    # ================================================================== #
    @app.post("/api/tx/submit")
    def tx_submit():
        data = request.get_json(force=True, silent=True) or {}
        try:
            tx = Transaction.from_dict(data)
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": f"malformed tx: {e}"}, 400)
        ok, reason = node.submit_transaction(tx)
        return _json({"ok": ok, "reason": reason, "txid": tx.txid},
                     status=200 if ok else 400)

    @app.post("/api/tx/transfer")
    def tx_transfer():
        data = request.get_json(force=True, silent=True) or {}
        sender = data.get("from")
        to = data.get("to")
        try:
            amount = float(data.get("amount", 0))
            fee = float(data.get("fee", 0))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid amount/fee"}, 400)
        if not crypto.is_valid_address(sender) or not crypto.is_valid_address(to):
            return _json({"ok": False, "error": "invalid address"}, 400)
        tx, err = node.create_transfer(sender, to, amount, fee)
        if err:
            return _json({"ok": False, "error": err}, 400)
        ok, reason = node.submit_transaction(tx)
        return _json({"ok": ok, "reason": reason, "txid": tx.txid,
                      "amount": amount, "fee": fee},
                     status=200 if ok else 400)

    @app.get("/api/txpool")
    def txpool():
        txs = node.pending_transactions()
        return _json({
            "count": len(txs),
            "transactions": [tx.to_dict() for tx in txs],
        })

    @app.get("/api/txpool/<txid>")
    def txpool_detail(txid):
        tx = node.txpool.get(txid)
        if not tx:
            return _json({"ok": False, "error": "not found"}, 404)
        return _json({"ok": True, "transaction": tx.to_dict()})

    @app.delete("/api/txpool/<txid>")
    def txpool_remove(txid):
        return _json({"ok": node.txpool.remove(txid)})

    @app.post("/api/txpool/clear")
    def txpool_clear():
        node.txpool.clear()
        node.save_txpool()
        return _json({"ok": True})

    # ================================================================== #
    # Mining
    # ================================================================== #
    @app.post("/api/mine")
    def mine_now():
        data = request.get_json(force=True, silent=True) or {}
        status, message, height = node.mine_block(data.get("miner"))
        return _json({"ok": status == "extended", "status": status,
                      "message": message, "height": height})

    @app.post("/api/mining/start")
    def mining_start():
        return _json({"ok": True, "mining": node.start_mining()})

    @app.post("/api/mining/stop")
    def mining_stop():
        return _json({"ok": True, "mining": not node.stop_mining()})

    @app.get("/api/mining/status")
    def mining_status():
        return _json({"mining": node.mining, "hashrate": round(node.hashrate, 1),
                      "last_duration": round(node._last_mine_duration, 4),
                      "last_attempts": node._last_mine_attempts})

    # ================================================================== #
    # Chain / explorer
    # ================================================================== #
    @app.get("/api/chain")
    def chain_info():
        bc = node.blockchain
        blocks = bc.chain_summary()
        return _json({"height": bc.height, "chainwork": bc.chainwork,
                      "genesis_difficulty": bc.genesis_difficulty,
                      "blocks": blocks})

    @app.get("/api/blocks")
    def blocks_list():
        bc = node.blockchain
        try:
            start = int(request.args.get("from", 0))
            end = int(request.args.get("to", bc.height))
        except ValueError:
            return _json({"error": "bad range"}, 400)
        start = max(0, start)
        end = min(bc.height, end)
        out = []
        for h in range(start, end + 1):
            b = bc.get_block(h)
            out.append(bc.block_summary(b))
        return _json({"blocks": out, "from": start, "to": end})

    @app.get("/api/block/<identifier>")
    def block_detail(identifier):
        bc = node.blockchain
        block = None
        if identifier.isdigit():
            block = bc.get_block(int(identifier))
        else:
            block = bc.get_block_by_hash(identifier)
        if not block:
            return _json({"ok": False, "error": "block not found"}, 404)
        return _json({"ok": True, "block": block.to_dict()})

    @app.get("/api/tx/<txid>")
    def tx_detail(txid):
        bc = node.blockchain
        for blk in bc.chain:
            for tx in blk.transactions:
                if tx.txid == txid:
                    return _json({"ok": True, "transaction": tx.to_dict(),
                                  "block": {
                                      "index": blk.index, "hash": blk.hash,
                                      "timestamp": blk.timestamp,
                                  }})
        return _json({"ok": False, "error": "transaction not found"}, 404)

    @app.get("/api/chain/validate")
    def chain_validate():
        report = node.blockchain.validate_full_chain()
        return _json(report)

    @app.get("/api/chain/forks")
    def chain_forks():
        bc = node.blockchain
        forks = []
        for height, blocks in bc.fork_store.items():
            for b in blocks:
                forks.append({"height": height, "hash": b.hash,
                              "prev_hash": b.prev_hash,
                              "difficulty": b.difficulty})
        return _json({"forks": forks, "last_abandoned": [
            b.index for b in bc.last_abandoned]})

    # ================================================================== #
    # Contracts
    # ================================================================== #
    @app.post("/api/contract/validate")
    def contract_validate():
        from .sandbox import validate_source
        data = request.get_json(force=True, silent=True) or {}
        ok, msg = validate_source(data.get("code", ""))
        return _json({"ok": ok, "message": msg})

    @app.post("/api/contract/deploy")
    def contract_deploy():
        data = request.get_json(force=True, silent=True) or {}
        sender = data.get("sender")
        code = data.get("code", "")
        if not crypto.is_valid_address(sender):
            return _json({"ok": False, "error": "invalid sender"}, 400)
        try:
            fee = float(data.get("fee", 0))
        except (TypeError, ValueError):
            fee = 0
        constructor = data.get("constructor")
        tx, err = node.create_deploy(sender, code, fee, constructor=constructor)
        if err:
            return _json({"ok": False, "error": err}, 400)
        ok, reason = node.submit_transaction(tx)
        address = "0xc" + crypto.sha256(tx.txid.encode()).hex()[:40]
        return _json({"ok": ok, "reason": reason, "txid": tx.txid,
                      "address": address, "fee": fee},
                     status=200 if ok else 400)

    @app.get("/api/contract/list")
    def contract_list():
        st = node.blockchain.state
        out = []
        for addr, c in st.contracts.items():
            out.append({
                "address": addr, "creator": c.get("creator"),
                "code": c["code"],
                "storage": c["storage"],
                "balance": st.balance(addr),
                "storage_keys": len(c["storage"]),
            })
        return _json({"contracts": out})

    @app.get("/api/contract/<addr>")
    def contract_detail(addr):
        st = node.blockchain.state
        c = st.contract(addr)
        if not c:
            return _json({"ok": False, "error": "contract not found"}, 404)
        events = node.contract_events(addr)
        return _json({
            "ok": True, "address": addr, "creator": c.get("creator"),
            "code": c["code"], "storage": c["storage"],
            "balance": st.balance(addr), "events": events[-200:],
        })

    @app.post("/api/contract/<addr>/call")
    def contract_call(addr):
        """Read-only simulation: does not mutate state or create a transaction."""
        data = request.get_json(force=True, silent=True) or {}
        sender = data.get("sender") or ZERO_ADDRESS
        result = node.blockchain.engine.simulate(
            addr, data.get("function", ""), data.get("args", []), sender,
            node.blockchain.state, node.blockchain.height)
        return _json(result)

    @app.post("/api/contract/<addr>/invoke")
    def contract_invoke(addr):
        data = request.get_json(force=True, silent=True) or {}
        sender = data.get("sender")
        if not crypto.is_valid_address(sender):
            return _json({"ok": False, "error": "invalid sender"}, 400)
        try:
            value = float(data.get("value", 0))
            fee = float(data.get("fee", 0))
        except (TypeError, ValueError):
            value = 0
            fee = 0
        tx, err = node.create_call(sender, addr, data.get("function", ""),
                                   data.get("args", []), fee=fee, value=value)
        if err:
            return _json({"ok": False, "error": err}, 400)
        ok, reason = node.submit_transaction(tx)
        if ok and data.get("mine"):
            node.mine_block(sender)
        return _json({"ok": ok, "reason": reason, "txid": tx.txid},
                     status=200 if ok else 400)

    @app.get("/api/contract/<addr>/events")
    def contract_events(addr):
        events = read_json(node.paths.contract_path(addr), {}).get("events", [])
        return _json({"events": events})

    # ================================================================== #
    # Templates
    # ================================================================== #
    def _custom_templates():
        return read_json(os.path.join(node.paths.root,
                                      "custom_templates.json"), [])

    @app.get("/api/templates")
    def templates_list():
        custom = _custom_templates()
        catalog = template_catalog()
        for t in custom:
            catalog.append({
                "name": t["name"], "title": t["title"],
                "category": t.get("category", "自定义"),
                "description": t.get("description", ""),
                "constructor": t.get("constructor", []),
                "functions": t.get("functions", []),
                "custom": True,
            })
        return _json({"templates": catalog})

    @app.get("/api/templates/<name>")
    def template_detail(name):
        t = get_template(name)
        custom = None
        if not t:
            custom = next((c for c in _custom_templates()
                           if c.get("name") == name), None)
        if not t and not custom:
            return _json({"ok": False, "error": "not found"}, 404)
        return _json({"ok": True, "template": t or custom})

    @app.post("/api/templates/save")
    def template_save():
        data = request.get_json(force=True, silent=True) or {}
        name = data.get("name", "")
        # Store custom templates alongside the built-in list.
        custom = read_json(os.path.join(node.paths.root, "custom_templates.json"), [])
        custom = [t for t in custom if t.get("name") != name]
        custom.append({
            "name": name, "title": data.get("title", name),
            "category": "自定义", "description": data.get("description", ""),
            "constructor": data.get("constructor", []),
            "functions": data.get("functions", []),
            "source": data.get("source", ""),
        })
        atomic_write_json(os.path.join(node.paths.root,
                                       "custom_templates.json"), custom)
        return _json({"ok": True, "name": name})

    # ================================================================== #
    # Stats
    # ================================================================== #
    @app.get("/api/stats/overview")
    def stats_overview():
        bc = node.blockchain
        blocks = bc.chain
        total_tx = sum(len(b.transactions) for b in blocks)
        counts = bc.tx_type_counts()
        transfers = counts.get("transfer", 0)
        calls = counts.get("call", 0)
        deploys = counts.get("deploy", 0)
        avg_interval = bc.avg_block_interval(20)
        top = bc.top_accounts(10)
        return _json({
            "height": bc.height, "total_blocks": len(blocks),
            "total_tx": total_tx, "transfers": transfers, "calls": calls,
            "deploys": deploys, "accounts": len(bc.state.accounts),
            "contracts": len(bc.state.contracts),
            "txpool": node.txpool.size(), "chainwork": bc.chainwork,
            "avg_block_interval": round(avg_interval, 2),
            "hashrate": round(node.hashrate, 1),
            "top_addresses": top,
        })

    @app.get("/api/stats/difficulty")
    def stats_difficulty():
        bc = node.blockchain
        return _json({"series": bc.difficulty_series()})

    @app.get("/api/stats/throughput")
    def stats_throughput():
        bc = node.blockchain
        return _json({"series": [
            {"index": b.index, "tx_count": len(b.transactions),
             "timestamp": b.timestamp} for b in bc.chain]})

    # ================================================================== #
    # Admin
    # ================================================================== #
    @app.get("/api/admin/versions")
    def admin_versions():
        return _json({"versions": node.blockchain.versions.all()})

    @app.post("/api/admin/rollback")
    def admin_rollback():
        data = request.get_json(force=True, silent=True) or {}
        try:
            height = int(data.get("height", 0))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid height"}, 400)
        # Re-admit transactions from blocks being rolled back.
        abandoned_blocks = node.blockchain.chain[height + 1:]
        ok, msg = node.blockchain.rollback(height)
        if not ok:
            return _json({"ok": False, "error": msg}, 400)
        for blk in abandoned_blocks:
            for tx in blk.transactions:
                if not tx.is_coinbase():
                    node.txpool.re_admit([tx])
        node.save_txpool()
        node.sync_contract_files()
        node.log("warn", f"admin rollback to height {height}")
        return _json({"ok": True, "message": msg, "height": height})

    @app.post("/api/admin/reset")
    def admin_reset():
        import shutil
        for sub in ("blocks", "state", "contracts"):
            shutil.rmtree(os.path.join(node.paths.root, sub), ignore_errors=True)
            os.makedirs(os.path.join(node.paths.root, sub), exist_ok=True)
        node.blockchain = _fresh_blockchain(node)
        node.txpool.clear()
        node.save_txpool()
        node.log("warn", "chain reset to genesis")
        return _json({"ok": True, "height": 0})

    @app.post("/api/admin/validate")
    def admin_validate():
        return _json(node.blockchain.validate_full_chain())

    @app.get("/api/admin/logs")
    def admin_logs():
        return _json({"logs": node.logs(limit=int(request.args.get("limit", 100)))})

    @app.get("/api/admin/status")
    def admin_status():
        return _json({
            "node": node_info().get_json(),
            "status": node_status().get_json(),
            "chainwork": node.blockchain.chainwork,
            "fork_store": {str(k): len(v) for k, v in
                           node.blockchain.fork_store.items()},
        })

    # ================================================================== #
    # P2P (node-to-node)
    # ================================================================== #
    @app.get("/p2p/status")
    def p2p_status():
        bc = node.blockchain
        return _json({
            "id": node.node_id, "height": bc.height,
            "head_hash": bc.head.hash if bc.head else None,
            "chainwork": bc.chainwork, "state_root": bc.state.root(),
            "genesis_difficulty": bc.genesis_difficulty,
        })

    @app.get("/p2p/blocks")
    def p2p_blocks():
        bc = node.blockchain
        try:
            start = int(request.args.get("from", 0))
            end = int(request.args.get("to", bc.height))
        except ValueError:
            return _json({"blocks": []}, 400)
        start = max(0, start)
        end = min(bc.height, end)
        blocks = [bc.get_block(h).to_dict() for h in range(start, end + 1)]
        return _json({"blocks": blocks, "from": start, "to": end})

    @app.post("/p2p/block")
    def p2p_block():
        data = request.get_json(force=True, silent=True) or {}
        try:
            status, message = node.receive_block(data)
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": status in ("extended", "reorg"), "status": status,
                      "message": message})

    @app.post("/p2p/tx")
    def p2p_tx():
        data = request.get_json(force=True, silent=True) or {}
        ok, reason = node.receive_tx(data)
        return _json({"ok": ok, "reason": reason})

    @app.post("/p2p/announce")
    def p2p_announce():
        data = request.get_json(force=True, silent=True) or {}
        node.peers.add(data.get("id"), data.get("host", "127.0.0.1"),
                       int(data.get("port", 8000)))
        return _json({"ok": True})

    return app


def _fresh_blockchain(node):
    from .blockchain import Blockchain
    bc = Blockchain(node.cfg, node.paths)
    bc.create_genesis()
    return bc
