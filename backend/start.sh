#!/bin/bash
# Start the Intain Admin AI backend server

cd "$(dirname "$0")"

echo "Starting Intain Admin AI Backend..."
echo "API Docs: http://localhost:8010/docs"
echo ""

python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 8010 \
    --reload \
    --reload-exclude "logs/*" \
    --reload-exclude "logs/**" \
    --reload-exclude "data/*" \
    --reload-exclude "data/**" \
    --log-level info
