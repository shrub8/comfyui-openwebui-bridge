"""
ComfyUI Cloud ↔ Open WebUI Bridge

Translates Open WebUI's local ComfyUI expectations into ComfyUI Cloud API calls:
- Injects X-API-Key on all requests
- Rewrites paths: /xxx → /api/xxx
- Bridges WebSocket completion events ↔ Cloud polling
- Proxies /view with redirect following
"""

import asyncio
import json
import os
import httpx
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("comfyui-bridge")

CLOUD_BASE = os.environ.get("COMFYUI_CLOUD_URL", "https://cloud.comfy.org")
API_KEY = os.environ.get("COMFYUI_API_KEY", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "2.0"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT_SECONDS", "300.0"))

app = FastAPI()


def cloud_headers():
    return {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }


@app.websocket("/ws")
async def websocket_bridge(websocket: WebSocket):
    """
    Open WebUI connects here and waits for a completion event.
    We poll ComfyUI Cloud and fire the expected WS messages when done.
    """
    await websocket.accept()
    log.info("WebSocket client connected")

    # Track prompt_ids submitted during this WS session
    pending: dict[str, asyncio.Task] = {}
    client_connected = True

    async def send_if_connected(data: dict):
        """Send message only if websocket is still connected."""
        if client_connected:
            try:
                await websocket.send_json(data)
                return True
            except Exception as e:
                log.warning(f"Failed to send message: {e}")
                return False
        return False

    async def poll_and_notify(prompt_id: str):
        """Poll Cloud until job completes, then send WS events."""
        log.info(f"Polling for prompt_id={prompt_id}")
        elapsed = 0.0
        async with httpx.AsyncClient() as client:
            while elapsed < POLL_TIMEOUT:
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
                try:
                    r = await client.get(
                        f"{CLOUD_BASE}/api/job/{prompt_id}/status",
                        headers=cloud_headers(),
                        timeout=10,
                    )
                    data = r.json()
                    status = data.get("status", "")
                    log.info(f"  {prompt_id} status={status}")

                    if status in ("completed", "success", "done"):
                        # Send execution_cached / executing null to mimic local ComfyUI
                        await send_if_connected({
                            "type": "execution_cached",
                            "data": {"nodes": [], "prompt_id": prompt_id}
                        })
                        await asyncio.sleep(0.5)  # Give Open WebUI time to process
                        await send_if_connected({
                            "type": "executing",
                            "data": {"node": None, "prompt_id": prompt_id}
                        })
                        # Keep connection alive briefly to ensure message is received
                        await asyncio.sleep(1.0)
                        log.info(f"Sent completion events for {prompt_id}")
                        return

                    elif status in ("failed", "cancelled", "error"):
                        await send_if_connected({
                            "type": "execution_error",
                            "data": {"prompt_id": prompt_id, "message": status}
                        })
                        return

                except Exception as e:
                    log.warning(f"Poll error: {e}")

        log.error(f"Timed out polling {prompt_id}")
        await send_if_connected({
            "type": "execution_error",
            "data": {"prompt_id": prompt_id, "message": "timeout"}
        })

    try:
        while True:
            # Open WebUI sends {"type": "subscribe", ...} or nothing meaningful
            # We just need to stay alive; prompt_ids come via HTTP /prompt
            # But we need a way to know which prompt_id to poll.
            # Open WebUI registers clientId in query params — we use that to
            # correlate with prompt submissions tracked in app state.
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                data = json.loads(msg)
                # If OWU sends a prompt_id hint
                if "prompt_id" in data:
                    pid = data["prompt_id"]
                    if pid not in pending:
                        pending[pid] = asyncio.create_task(poll_and_notify(pid))
            except asyncio.TimeoutError:
                pass  # normal, just keep alive
            except Exception:
                pass

            # Also check app-level pending queue (set by /prompt handler)
            for pid in list(app.state.pending_prompts):
                if pid not in pending:
                    pending[pid] = asyncio.create_task(poll_and_notify(pid))
                    app.state.pending_prompts.discard(pid)

    except WebSocketDisconnect:
        client_connected = False
        log.info("WebSocket client disconnected")
        for task in pending.values():
            task.cancel()


@app.post("/prompt")
async def submit_prompt(request: Request):
    """Forward prompt to Cloud, register prompt_id for WS polling."""
    body = await request.json()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{CLOUD_BASE}/api/prompt",
            headers=cloud_headers(),
            json=body,
            timeout=30,
        )
    data = r.json()
    prompt_id = data.get("prompt_id")
    if prompt_id:
        app.state.pending_prompts.add(prompt_id)
        log.info(f"Queued prompt_id={prompt_id} for WS notification")
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


@app.get("/history/{prompt_id}")
async def get_history(prompt_id: str):
    """
    Open WebUI expects GET /history/{prompt_id} to return output filenames.
    ComfyUI Cloud doesn't have this endpoint. We query /api/jobs instead
    and construct a synthetic history response.
    """
    # Query the jobs list to find this prompt_id
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/jobs",
            headers=cloud_headers(),
            timeout=15,
        )

    if r.status_code == 200:
        data = r.json()
        jobs = data.get("jobs", [])
        job = next((j for j in jobs if j.get("id") == prompt_id), None)

        if job and job.get("preview_output"):
            output = job["preview_output"]
            node_id = output.get("nodeId", "60")

            # Return synthetic history in format Open WebUI expects
            synthetic_history = {
                prompt_id: {
                    "outputs": {
                        node_id: {
                            "images": [
                                {
                                    "filename": output["filename"],
                                    "subfolder": output.get("subfolder", ""),
                                    "type": output.get("type", "output")
                                }
                            ]
                        }
                    }
                }
            }
            return Response(
                content=json.dumps(synthetic_history),
                media_type="application/json",
                status_code=200
            )

    # Fallback: try the standard endpoint (for local ComfyUI compatibility)
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/history/{prompt_id}",
            headers=cloud_headers(),
            timeout=15,
        )

    if r.status_code == 404:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{CLOUD_BASE}/history/{prompt_id}",
                headers=cloud_headers(),
                timeout=15,
            )

    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


@app.get("/view")
async def view_image(request: Request):
    """Proxy image download, following the 302 redirect from Cloud."""
    params = dict(request.query_params)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/view",
            headers={k: v for k, v in cloud_headers().items() if k != "Content-Type"},
            params=params,
            timeout=60,
        )
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/png"),
        status_code=200,
    )


@app.get("/object_info")
async def object_info():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/object_info",
            headers=cloud_headers(),
            timeout=15,
        )
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


@app.get("/system_stats")
async def system_stats():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/system_stats",
            headers=cloud_headers(),
            timeout=15,
        )
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


@app.get("/queue")
async def queue():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{CLOUD_BASE}/api/queue",
            headers=cloud_headers(),
            timeout=15,
        )
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


@app.on_event("startup")
async def startup():
    app.state.pending_prompts = set()
    log.info(f"Bridge started → {CLOUD_BASE}")
