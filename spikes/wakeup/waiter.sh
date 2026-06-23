#!/bin/bash
# Throwaway: sleep, then emit an opaque envelope and exit.
SECS="${1:-20}"
sleep "$SECS"
echo "nelix_event spikeA evt-$(date +%s)"
