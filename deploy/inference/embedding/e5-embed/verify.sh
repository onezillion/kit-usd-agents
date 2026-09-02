#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://192.168.100.196:8000}"

printf 'Endpoint: %s\n' "$base_url"

printf '\nHealth:\n'
curl -fsS "$base_url/v1/health/ready"
printf '\n'

printf '\nModels:\n'
curl -fsS "$base_url/v1/models"
printf '\n'

printf '\nEmbedding compatibility:\n'
curl -fsS \
  "$base_url/v1/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "nvidia/nv-embedqa-e5-v5",
    "input": ["clash detection"],
    "input_type": "query",
    "modality": "text",
    "encoding_format": "float"
  }' |
python3 -c '
import json
import sys

result = json.load(sys.stdin)
model = result.get("model")
dimensions = len(result["data"][0]["embedding"])

print("model:", model)
print("dimensions:", dimensions)

if model != "nvidia/nv-embedqa-e5-v5":
    raise SystemExit("Unexpected model")

if dimensions != 1024:
    raise SystemExit("Unexpected embedding dimensions")

print("E5_COMPATIBILITY_OK")
'
