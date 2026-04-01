"""
WebSocket handler for live job progress streaming.
Clients connect to /ws/jobs/{job_id} and receive ProgressEvent
JSON objects as the scraping job runs.
"""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from backend.core.jobs import JobManager
from backend.core.logger import get_logger

logger = get_logger("api.websocket")


async def job_progress_websocket(websocket: WebSocket, job_id: str):
    """
    WebSocket endpoint handler for streaming job progress events.
    Connects to the JobManager's progress queue for the given job
    and forwards events to the client as JSON.
    """
    await websocket.accept()
    logger.info(f"WebSocket connected for job {job_id}")

    manager = JobManager()
    queue = manager.get_progress_queue(job_id)

    if queue is None:
        # job doesn't exist or already cleaned up
        await websocket.send_json(
            {
                "event_type": "error",
                "message": f"Job {job_id} not found",
            }
        )
        await websocket.close()
        return

    try:
        while True:
            try:
                # wait for the next progress event with a timeout
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event.to_dict())

                # if the job finished, send the final event and close
                if event.event_type in ("completed", "failed"):
                    logger.info(f"WebSocket closing: job {job_id} {event.event_type}")
                    break

            except asyncio.TimeoutError:
                # send a heartbeat to keep the connection alive
                job = manager.get_job_status(job_id)
                if job is None:
                    await websocket.send_json(
                        {"event_type": "error", "message": "Job not found"}
                    )
                    break
                elif job.status in ("completed", "failed", "cancelled"):
                    await websocket.send_json(
                        {
                            "event_type": job.status,
                            "message": job.message,
                            "count_found": job.count_found,
                        }
                    )
                    break
                else:
                    # job still running, send heartbeat
                    await websocket.send_json(
                        {
                            "event_type": "heartbeat",
                            "message": job.message,
                            "count_found": job.count_found,
                        }
                    )

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for job {job_id}")
    except Exception as exc:
        logger.error(f"WebSocket error for job {job_id}: {exc}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
