#!/usr/bin/env bash
# Phase 0 bundler - HILLTOP-RUNBOOK.md 0.2.
# Builds meshtech-node-deploy-YYYYMMDD.tar.gz from the repo root:
#   bash deploy/make-package.sh
# Contents: node src, cleanmodem, deploy templates, app dist, docs.
# NO secrets: the #scope key is the public hashtag rule; the modem
# token file is created ON the box (runbook 1.4).
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date +%Y%m%d)
OUT="meshtech-node-deploy-${STAMP}.tar.gz"

tar -czf "$OUT" \
    --transform 's|^src|meshtech-node/src|' \
    --transform 's|^cleanmodem|meshtech-node/cleanmodem|' \
    --transform 's|^deploy|meshtech-node/deploy|' \
    --transform 's|^pyproject.toml|meshtech-node/pyproject.toml|' \
    --transform 's|^scope-app/dist|meshtech-node/app|' \
    src/meshtech_node \
    cleanmodem \
    deploy \
    pyproject.toml \
    ../scope-app/dist \
    PLAN.md BENCH-CHECKLIST.md HILLTOP-RUNBOOK.md SEED-MAP.md

echo "---"
echo "Built $OUT:"
tar -tzf "$OUT" | head -8
echo "  ... ($(tar -tzf "$OUT" | wc -l) entries total)"
echo "SHA256:"
sha256sum "$OUT"
