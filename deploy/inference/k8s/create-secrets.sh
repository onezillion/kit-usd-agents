#!/usr/bin/env bash
set -euo pipefail

: "${NGC_SVC_KEY:?NGC_SVC_KEY is not set}"
: "${NGC_PSN_KEY:?NGC_PSN_KEY is not set}"
: "${NGC_LGA_KEY:?NGC_LGA_KEY is not set}"
: "${TAIWAN_AI_API_KEY:?TAIWAN_AI_API_KEY is not set}"

kubectl create namespace kit-ai \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl -n kit-ai create secret generic ngc-service \
  --from-literal=NGC_SVC_KEY="$NGC_SVC_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl -n kit-ai create secret generic ngc-personal \
  --from-literal=NGC_PSN_KEY="$NGC_PSN_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl -n kit-ai create secret generic ngc-legacy \
  --from-literal=NGC_LGA_KEY="$NGC_LGA_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl -n kit-ai create secret generic taiwan-ai \
  --from-literal=TAIWAN_AI_API_KEY="$TAIWAN_AI_API_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

# Legacy key is currently the proven working NIM credential.
kubectl -n kit-ai create secret generic ngc-runtime \
  --from-literal=NGC_API_KEY="$NGC_LGA_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl -n kit-ai create secret docker-registry ngc-pull \
  --docker-server=nvcr.io \
  --docker-username='$oauthtoken' \
  --docker-password="$NGC_LGA_KEY" \
  --dry-run=client -o yaml |
kubectl apply -f -

kubectl get secrets -n kit-ai \
  -o custom-columns=NAME:.metadata.name,TYPE:.type
