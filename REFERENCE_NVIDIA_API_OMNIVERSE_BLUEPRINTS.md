# NVIDIA Build API and Omniverse Blueprints Reference

## Purpose

Persistent reference for future local AI / agent / MCP / Omniverse work.

## NVIDIA Build API discovery

The NVIDIA Build/API catalog is much broader than the few models initially visible on the first discovery screen. Many models expose NVIDIA-hosted **Free Endpoint** access, and some are also **Downloadable**.

Observed API patterns include an OpenAI-compatible endpoint:

```text
https://integrate.api.nvidia.com/v1
```

Typical chat endpoint:

```text
POST https://integrate.api.nvidia.com/v1/chat/completions
Authorization: Bearer $NVIDIA_API_KEY
```

The same `NVIDIA_API_KEY` generated from build.nvidia.com was successfully used by the Kit MCP NVIDIA-hosted reranker.

Examples observed in the Build catalog include:

- `moonshotai/kimi-k3` — multimodal / coding / agentic use; Free Endpoint + Downloadable
- `deepseek-ai/deepseek-v4-pro-0813` — coding, long context; Free Endpoint
- `deepseek-ai/deepseek-v4-flash-0731` — coding/chat/agentic; Free Endpoint
- `nvidia/nemotron-3.5-lightning-30b-a3b` — agentic tasks; Free Endpoint + Downloadable
- `meta/muse-glimmer-30b` — multimodal reasoning + tool calling; Free Endpoint + Downloadable
- `nvidia/nemotron-3-embed-1b` — embeddings/RAG; Free Endpoint
- `poolside/laguna-xs-2.1` — agentic coding/terminal tasks; Free Endpoint
- `nvidia/nemotron-3-ultra-550b-a55b` — reasoning/coding/planning/tool calling; Free Endpoint + Downloadable
- `nvidia/cosmos3-nano` — physics-aware video generation; Free Endpoint + Downloadable
- `nvidia/cosmos3-nano-reasoner` — physical-world video/image reasoning; Free Endpoint + Downloadable
- `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` — image/video/speech/text reasoning; Free Endpoint + Downloadable

Important implication: do not assume the NVIDIA hosted API is limited to only a few small trial models. Re-check the Build catalog when selecting hosted models for agents, coding, multimodal tasks, embeddings, reranking, or experimentation.

Catalog:
https://build.nvidia.com/explore/discover

## Kit MCP reranker result

During VS Code Kit MCP validation, the old hosted reranker endpoint:

```text
nvidia/llama-nemotron-rerank-1b-v2
```

returned HTTP `410 Gone`.

For active `kit_fns`, it was updated to:

```text
nvidia/llama-nemotron-rerank-vl-1b-v2
https://ai.api.nvidia.com/v1/retrieval/nvidia/llama-nemotron-rerank-vl-1b-v2/reranking
```

The existing request payload remained compatible. End-to-end test passed with:

```text
Creating reranker with backend: nvidia_api
Using NVIDIA API reranker
Reranker initialized for Kit code examples
Successfully found 10 code examples
```

Current hybrid search design:

```text
VS Code Agent
  -> Kit MCP
  -> local E5 embedder
  -> FAISS retrieval
  -> NVIDIA hosted reranker
  -> ranked results
```

Shared local config/secrets are loaded from:

```text
/home/ubuntu/Documents/kit-usd-agents/source/mcp/.env
```

with local embeddings plus `NVIDIA_API_KEY`. Never commit or paste the API-key value.

## NVIDIA Omniverse Blueprints

Keep this organization as a reference source for reusable examples, architecture patterns, agent workflows, and Omniverse integrations:

https://github.com/NVIDIA-Omniverse-blueprints

The examples there look relevant to future work on local AI/agents/MCP/agent deployment and extending Omniverse/Kit applications. Review individual repositories as needed rather than copying wholesale.

## Future follow-up ideas

- Inventory NVIDIA Build Free Endpoint models relevant to our Kit/Omniverse agent stack.
- Compare hosted models against locally deployed NIM models for latency, quality, privacy, and cost.
- Review NVIDIA-Omniverse-blueprints repositories for reusable agent/MCP/Kit patterns.
- Consider using hosted endpoints for burst/experimental capabilities while keeping embeddings and core runtime local.
- Periodically verify hosted model availability because Build API models/endpoints can be added, deprecated, or renamed.
