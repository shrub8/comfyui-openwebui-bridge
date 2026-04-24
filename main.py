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
    WebSocket endpoint. Open WebUI connects here and waits for completion events.

    Design:
    - Immediately send an initial 'status' message so the connection is definitively alive
    - Wait for a prompt_id to appear in app.state.pending_prompts (set by /prompt handler)
    - Start polling Cloud and send events directly
    - Keep WS alive with periodic pings (raw WS ping frames)
    """
    await websocket.accept()
    client_id = websocket.query_params.get("clientId", "unknown")
    log.info(f"WebSocket client connected: clientId={client_id}")

    # Send an initial status message to confirm connection
    try:
        await websocket.send_json({
            "type": "status",
            "data": {"status": {"exec_info": {"queue_remaining": 0}}, "sid": client_id}
        })
    except Exception as e:
        log.warning(f"Initial send failed: {e}")
        return

    # Track associations
    handled_prompts = set()
    receive_task = None

    async def drain_receive():
        """Drain any messages sent by client to keep connection responsive."""
        try:
            while True:
                msg = await websocket.receive_text()
                log.debug(f"Client sent: {msg[:100]}")
        except WebSocketDisconnect:
            log.info(f"Client {client_id} disconnected")
        except Exception as e:
            log.warning(f"Receive error: {e}")

    async def poll_and_notify(prompt_id: str):
        """Poll Cloud until job completes, send WS events directly."""
        log.info(f"[{client_id}] Polling for prompt_id={prompt_id}")
        elapsed = 0.0
        async with httpx.AsyncClient() as client:
            # Send execution_start immediately
            try:
                await websocket.send_json({
                    "type": "execution_start",
                    "data": {"prompt_id": prompt_id}
                })
            except Exception as e:
                log.warning(f"Failed execution_start: {e}")
                return

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
                    log.info(f"[{client_id}] {prompt_id} status={status}")

                    if status in ("completed", "success", "done"):
                        try:
                            await websocket.send_json({
                                "type": "execution_cached",
                                "data": {"nodes": [], "prompt_id": prompt_id}
                            })
                            await asyncio.sleep(0.3)
                            await websocket.send_json({
                                "type": "executing",
                                "data": {"node": None, "prompt_id": prompt_id}
                            })
                            log.info(f"[{client_id}] Sent completion for {prompt_id}")
                        except Exception as e:
                            log.error(f"Failed to send completion: {e}")
                        return

                    elif status in ("failed", "cancelled", "error"):
                        try:
                            await websocket.send_json({
                                "type": "execution_error",
                                "data": {"prompt_id": prompt_id, "message": status}
                            })
                        except Exception:
                            pass
                        return

                    else:
                        # Progress update
                        try:
                            await websocket.send_json({
                                "type": "progress",
                                "data": {"value": 1, "max": 2, "prompt_id": prompt_id, "node": None}
                            })
                        except Exception as e:
                            log.warning(f"Heartbeat failed: {e}")
                            return

                except Exception as e:
                    log.warning(f"Poll error: {e}")

            log.error(f"[{client_id}] Timeout polling {prompt_id}")

    # Start receive drain in background
    receive_task = asyncio.create_task(drain_receive())

    try:
        # Main loop: watch for pending_prompts and handle them
        while True:
            await asyncio.sleep(0.2)

            # Check for new prompts to handle
            for pid in list(app.state.pending_prompts):
                if pid not in handled_prompts:
                    handled_prompts.add(pid)
                    app.state.pending_prompts.discard(pid)
                    # Run poll_and_notify directly - don't spawn task so we await it
                    await poll_and_notify(pid)
                    # After completion, we're done with this WS
                    log.info(f"[{client_id}] Poll finished for {pid}, keeping WS open")

            if receive_task.done():
                log.info(f"[{client_id}] Receive task ended, closing WS loop")
                break

    except Exception as e:
        log.error(f"[{client_id}] Main loop error: {e}")
    finally:
        if receive_task and not receive_task.done():
            receive_task.cancel()
        log.info(f"[{client_id}] WebSocket handler exiting")

@app.post("/prompt")
async def submit_prompt(request: Request):
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
        log.info(f"Queued prompt_id={prompt_id}")
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)

@app.get("/history/{prompt_id}")
async def get_history(prompt_id: str):
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

    return Response(content=b"{}", media_type="application/json", status_code=200)

@app.get("/view")
async def view_image(request: Request):
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
