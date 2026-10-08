#!/bin/bash
# Double-click to start Molly Remote and open it in your browser. Close this window to stop it.
cd "$(dirname "$0")"
(sleep 1.5; open "http://localhost:8686") &
exec python3 server.py
