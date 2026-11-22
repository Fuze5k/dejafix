#!/bin/sh
# Start the local Meko server with every usage-stats channel off: Meko's own (MEKO_CALL_HOME,
# YB_DISABLE_CALLHOME) and ONNX Runtime's, which the context engine loads and which otherwise reports
# to Microsoft (events.data.microsoft.com). Runs the release binary by its real path: started through
# the ~/.local/bin/meko link, Meko 1.20261116.0 on macOS looks for its context engine next to the link.
set -eu
export MEKO_CALL_HOME=false YB_DISABLE_CALLHOME=1 ORT_DISABLE_TELEMETRY=1
exec "$HOME/.local/share/meko/app/current/bin/meko" start "$@"
