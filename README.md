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
| `POST /prompt` | Forwards with `X-API-Key` | `POST /api/prompt` |
| `GET /history/{id}` | Queries `/api/jobs`, returns synthetic history | `GET /api/jobs` (list) |
| `GET /view?filename=...` | Proxies to cloud with auth, follows 302 | `GET /api/view` (with hashed names) |
| `GET /object_info`, `/system_stats`, `/queue` | Proxied with `X-API-Key` | `/api/*` equivalents |
| `Authorization: Bearer` (ignored) | Bridge uses its own `X-API-Key` | `X-API-Key` header |

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
  --namespace=open-webui \
  --from-literal=api-key=YOUR_COMFYUI_CLOUD_API_KEY

# Apply the deployment
kubectl apply -f deployment.yaml
```

See `deployment.yaml` for the full Kubernetes manifest.

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

## Open WebUI Configuration

In Open WebUI Admin Settings → Images:

- **Image Generation Engine:** `ComfyUI`
- **ComfyUI Base URL:** `http://comfyui-proxy.open-webui.svc.cluster.local:8188` (or wherever you deployed the bridge)
- **ComfyUI API Key:** Any non-empty string (the bridge ignores this — it uses its own `COMFYUI_API_KEY` env var)
- **ComfyUI Workflow:** Your standard workflow JSON (e.g. Qwen-Image, SDXL, etc.)

## How It Works

### 1. Prompt Submission

Open WebUI sends workflow JSON to `POST /prompt`.
Bridge forwards to `POST /api/prompt` on ComfyUI Cloud with the `X-API-Key` header.
Cloud returns a `prompt_id` which the bridge tracks in `app.state.pending_prompts`.

### 2. Completion Detection

Open WebUI connects to WebSocket `/ws`.
Bridge starts polling `GET /api/job/{prompt_id}/status` every 2 seconds.
When status is `success` (or `completed`/`done`), bridge sends fake WebSocket messages:

```json
{"type": "execution_cached", "data": {"nodes": [], "prompt_id": "..."}}
{"type": "executing", "data": {"node": null, "prompt_id": "..."}}
```

These are the exact messages Open WebUI waits for to know generation is complete.

During execution, `progress` messages are sent periodically to keep the WebSocket alive.

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
Follows 302 redirects (ComfyUI Cloud redirects to a signed Google Cloud Storage URL).

## WebSocket Handler Design

The WebSocket handler uses a dual-task architecture to avoid ASGI state confusion:

1. **Receive drain task** (background) — continuously drains any messages from the client. This keeps the WebSocket responsive and detects real disconnects via `WebSocketDisconnect`.
2. **Main poll loop** — watches `app.state.pending_prompts` for new prompts and drives the poll-and-notify flow directly. Never calls `receive_*` itself.

This design fixed a subtle bug: the earlier version used `receive_text(timeout=1.0)` in the main loop to check for pending prompts. That timeout caused uvicorn/starlette to mark the connection as "response already completed", so every `send_json` after the first poll failed with:

```
Unexpected ASGI message 'websocket.send', after sending 'websocket.close' or response already completed.
```

The first image in a chat would still work (fresh connection state), but every subsequent image would show stale content because the completion events never reached Open WebUI. Splitting receive and send into separate tasks resolved this.

## Security Considerations

### Secrets Management

**Kubernetes (recommended):**
- API key stored in Kubernetes Secret (`comfyui-secret`)
- Mounted as env var via `secretKeyRef` in Deployment
- Never committed to Git

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
  --namespace=open-webui \
  --from-literal=api-key=YOUR_ACTUAL_KEY
```

### Network Exposure

The bridge should be **internal-only** (no public ingress). It should only be reachable from Open WebUI inside the cluster. Exposing it publicly would effectively expose your ComfyUI Cloud API key behind a thin proxy.

## Development

```bash
pip install fastapi 'uvicorn[standard]' httpx
export COMFYUI_API_KEY=your-key-here
uvicorn main:app --reload --port 8188
```

## Files

- `main.py` — FastAPI bridge application
- `Dockerfile` — Container build
- `deployment.yaml` — Kubernetes Deployment + Service
- `secret-example.yaml` — Example secret manifest (replace with real key)
- `README.md` — This file

## License

MIT
