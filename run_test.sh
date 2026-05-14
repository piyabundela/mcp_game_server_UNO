#!/bin/bash
# run_test.sh — Part 2: Run the automated UNO game test
set -e

cd "$(dirname "$0")"

echo "Running UNO MCP automated test..."
python test.py
