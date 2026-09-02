# E5 Embedding NIM for Kit Agents

Persistent local embedding service used by NVIDIA Kit MCP and related agents.

## Deployment

- Namespace: `kit-ai`
- Deployment: `e5-embed`
- Node: `witty-coyote`
- GPU: one RTX 4090
- Model: `nvidia/nv-embedqa-e5-v5`
- Image: PB6 `1.14.4-stig-fips-x86`
- Cluster DNS: `http://e5-embed.kit-ai.svc.cluster.local:8000`
- Private IP: `http://192.168.100.196:8000`
- Embedding dimensions: 1024

## Secrets

Cluster inference secrets are managed by:

```bash
../../k8s/create-secrets.sh