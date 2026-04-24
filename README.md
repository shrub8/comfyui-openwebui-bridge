# ComfyUI Cloud Bridge for Open WebUI

A FastAPI-based bridge service that translates Open WebUI's local ComfyUI expectations into ComfyUI Cloud API calls.

## Problem

Open WebUI expects a local ComfyUI instance with these behaviors:
- WebSocket `/ws` for real-time completion events
- `GET /history/{prompt_id}` to get output filenames
- `GET /view?filename=...` to download images
- `Authorization: Bearer` header for auth

ComfyUI Cloud uses different patterns:
- HTTP polling via `GET /api/job/{prompt_id}/status`
- `GET /api/jobs` to get output info (no `/history/` endpoint)
- Different auth header (`X-API-Key`)
- Hashed filenames that don't match the workflow node names

## Solution

This bridge sits between Open WebUI and ComfyUI Cloud, translating:

| Open WebUI Expects | Bridge Provides | Cloud Actually Has |
|-------------------|-----------------|-------------------|
| WebSocket `/ws` | Accepts WS, polls cloud internally | `GET /api/job/{id}/status` |
| `GET /history/{id}` | Queries `/api/jobs`, returns synthetic history | `GET /api/jobs` (list) |
| `GET /view?filename=...` | Proxies to cloud with auth | `GET /api/view` (with hashed names) |
| `Authorization: Bearer` | Ignored (bridge uses its own key) | `X-API-Key` header |

## Architecture

```
Open WebUI → Bridge (this app) → ComfyUI Cloud
     WS /ws        WS accept + poll      /api/job/{id}/status
     POST /prompt  POST /api/prompt       /api/prompt
     GET /history  GET /api/jobs → synth  /api/jobs
     GET /view     GET /api/view          /api/view
```

## Deployment

### Kubernetes (Recommended)

```bash
# Create secret for API key (DO NOT commit this file with real keys)
kubectl create secret generic comfyui-secret \
  --from-literal=api-key=YOUR_COMFYUI_CLOUD_API_KEY

# Apply the deployment
kubectl apply -f deployment.yaml
```

See `deployment.yaml` for full Kubernetes manifest.

### Docker

```bash
docker build -t comfyui-bridge .
docker run -e COMFYUI_API_KEY=YOUR_KEY -p 8188:8188 comfyui-bridge
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COMFYUI_CLOUD_URL` | No | `https://cloud.comfy.org` | ComfyUI Cloud base URL |
| `COMFYUI_API_KEY` | **Yes** | - | Your ComfyUI Cloud API key |
| `POLL_INTERVAL_SECONDS` | No | `2.0` | How often to poll job status |
| `POLL_TIMEOUT_SECONDS` | No | `300.0` | Max time to wait for job completion |

## How It Works

### 1. Prompt Submission

Open WebUI sends workflow JSON to `POST /prompt`.
Bridge forwards to `POST /api/prompt` on ComfyUI Cloud.
Cloud returns a `prompt_id` which the bridge tracks.

### 2. Completion Detection

Open WebUI connects to WebSocket `/ws`.
Bridge starts a background task that polls `GET /api/job/{prompt_id}/status` every 2 seconds.
When status is `success` (or `completed`/`done`), bridge sends fake WebSocket messages:

```json
{"type": "execution_cached", "data": {"nodes": [], "prompt_id": "..."}}
{"type": "executing", "data": {"node": null, "prompt_id": "..."}}
```

These are the exact messages Open WebUI waits for to know generation is complete.

### WebSocket Handler Design

The WebSocket handler uses a dual-task architecture to avoid ASGI state confusion:

1. **Receive drain task** (background) — continuously drains any messages from the client. This keeps the WebSocket responsive and detects real disconnects via `WebSocketDisconnect`.
2. **Main poll loop** — watches `app.state.pending_prompts` for new prompts and drives the poll-and-notify flow directly.

This design was critical to fix a bug where `receive_text(timeout=1.0)` in the main loop was causing uvicorn/starlette to mark the connection as "response already completed", resulting in all subsequent `send_json` calls failing after the first successful image.

### 3. History Lookup

Open WebUI requests `GET /history/{prompt_id}` expecting:
```json
{
  "prompt_id": {
    "outputs": {
      "node_id": {
        "images": [{"filename": "...", "subfolder": "", "type": "output"}]
      }
    }
  }
}
```

ComfyUI Cloud doesn't have `/history/`. The bridge queries `GET /api/jobs`, finds the job by `prompt_id`, extracts `preview_output` with the hashed filename, and returns a synthetic history in the exact format Open WebUI expects.

### 4. Image Download

Open WebUI requests `GET /view?filename=...&type=output`.
Bridge proxies this to `GET /api/view` on ComfyUI Cloud with the `X-API-Key` header.
Follows 302 redirects if needed.

## Security Considerations

### Secrets Management

**Current approach (Kubernetes):**
- API key stored in Kubernetes Secret (`comfyui-secret`)
- Mounted as env var via `secretKeyRef` in Deployment
- Never committed to Git

**For GitHub repo:**
```yaml
# deployment.yaml (safe to commit)
env:
  - name: COMFYUI_API_KEY
    valueFrom:
      secretKeyRef:
        name: comfyui-secret      # created separately
        key: api-key
```

```bash
# Create secret locally (DO NOT commit this)
kubectl create secret generic comfyui-secret \
  --from-literal=api-key=YOUR_ACTUAL_KEY
```

### Other Secrets in the Cluster

The bridge only needs `COMFYUI_API_KEY`. Other secrets in the `open-webui` namespace:
- `webui-tls` — TLS certificate (managed by cert-manager)
- `comfyui-secret` — This API key

## Development

```bash
pip install fastapi uvicorn[standard] httpx
uvicorn main:app --reload --port 8188
```

## Files

- `main.py` — FastAPI bridge application
- `Dockerfile` — Container build
- `deployment.yaml` — Kubernetes manifest
- `secret-example.yaml` — Example secret manifest (replace with real key)
- `README.md` — This file

## License

MIT
