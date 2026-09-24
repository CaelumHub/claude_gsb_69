#!/usr/bin/env bash
# Start a 3-node blockchain network on ports 8000-8002.
#   node1 (seed) auto-mines; node2 & node3 join it and stay in sync.
set -e

cd "$(dirname "$0")"
mkdir -p run

echo "==> starting node1 (seed, :8000)"
nohup python3 run.py --id node1 --port 8000 --seed --mine > run/node1.log 2>&1 &
echo $! > run/node1.pid

sleep 2
echo "==> starting node2 (:8001)"
nohup python3 run.py --id node2 --port 8001 --peers 127.0.0.1:8000 > run/node2.log 2>&1 &
echo $! > run/node2.pid

echo "==> starting node3 (:8002)"
nohup python3 run.py --id node3 --port 8002 --peers 127.0.0.1:8000,127.0.0.1:8001 > run/node3.log 2>&1 &
echo $! > run/node3.pid

echo "==> network up:"
echo "    node1: http://127.0.0.1:8000"
echo "    node2: http://127.0.0.1:8001"
echo "    node3: http://127.0.0.1:8002"
echo "    logs : run/node{1,2,3}.log   (stop with ./stop_network.sh)"
