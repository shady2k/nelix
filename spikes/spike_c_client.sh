#!/bin/bash
HOST="${1:?usage: spike_c_client.sh <host>}"
PORT="${NELIX_RPC_PORT:-8787}"
curl -fsS -H "X-Nelix-Token: ${NELIX_RPC_TOKEN:?set NELIX_RPC_TOKEN}" "http://$HOST:$PORT/ping" && echo
