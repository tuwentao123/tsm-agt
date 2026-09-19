from __future__ import annotations

import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from tsm_agt.local_api import LocalEventApiServer

app = FastAPI(title="tsm-agt-web")

INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset=\"utf-8\" />
<title>tsm-agt web ui</title>
<style>
body { background:#0b1020; color:#dbe4ff; font-family:Arial; margin:0; }
.layout { display:flex; height:100vh; }
.sidebar { width:260px; border-right:1px solid #223; padding:16px; }
.main { flex:1; display:flex; flex-direction:column; }
.timeline { flex:1; overflow:auto; padding:16px; }
.inputbar { display:flex; gap:8px; padding:16px; border-top:1px solid #223; }
.card { background:#131a2f; border-radius:12px; padding:12px; margin-bottom:12px; }
textarea { flex:1; background:#111827; color:white; border:1px solid #334155; border-radius:8px; padding:12px; }
button { background:#2563eb; color:white; border:none; border-radius:8px; padding:12px 16px; }
.preview-list { display:flex; gap:12px; padding:0 16px 16px; overflow:auto; }
.preview-item { background:#131a2f; border:1px solid #334155; border-radius:10px; padding:8px; min-width:140px; }
.preview-item img { width:100%; border-radius:6px; max-height:120px; object-fit:cover; }
.file-input { color:#dbe4ff; }
</style>
</head>
<body>
<div class=\"layout\">
<div class=\"sidebar\">
<h2>tsm-agt</h2>
<p>Web runtime timeline</p>
</div>
<div class=\"main\">
<div id=\"timeline\" class=\"timeline\"></div>
<div id=\"preview-list\" class=\"preview-list\"></div>
<div class=\"inputbar\">
<input id=\"image-input\" class=\"file-input\" type=\"file\" accept=\"image/*\" multiple onchange=\"handleImages(event)\" />
<textarea id=\"prompt\" placeholder=\"Ask the agent...\"></textarea>
<button onclick=\"runTask()\">Run</button>
</div>
</div>
</div>
<script>
const selectedImages = [];

async function fileToDataUrl(file) {
 return new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(reader.result);
  reader.onerror = reject;
  reader.readAsDataURL(file);
 });
}

async function handleImages(event) {
 const previewList = document.getElementById('preview-list');
 previewList.innerHTML = '';
 selectedImages.length = 0;

 for (const file of event.target.files) {
  const dataUrl = await fileToDataUrl(file);
  selectedImages.push({
   name: file.name,
   media_type: file.type,
   image_url: dataUrl,
  });

  previewList.innerHTML += `
   <div class='preview-item'>
    <img src='${dataUrl}' alt='${file.name}' />
    <div>${file.name}</div>
   </div>
  `;
 }
}

async function runTask() {
 const prompt = document.getElementById('prompt').value;
 const response = await fetch('/tasks', {
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({
   goal: prompt,
   images: selectedImages,
  })
 });
 const data = await response.json();
 const timeline = document.getElementById('timeline');
 timeline.innerHTML += `<div class='card'><b>Task</b><br/>${data.task_id}</div>`;
 const eventSource = new EventSource(`/stream/${data.task_id}`);
 eventSource.onmessage = (event) => {
   timeline.innerHTML += `<div class='card'>${event.data}</div>`;
   timeline.scrollTop = timeline.scrollHeight;
 };
}
</script>
</body>
</html>
"""

runtime_server: LocalEventApiServer | None = None


@app.on_event("startup")
async def startup() -> None:
    global runtime_server
    runtime_server = LocalEventApiServer(Path.cwd())
    runtime_server.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if runtime_server is not None:
        runtime_server.stop()


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.post("/tasks")
async def create_task(payload: dict) -> dict:
    if runtime_server is None:
        raise RuntimeError("runtime unavailable")

    goal = str(payload.get("goal", ""))
    image_blocks = []
    for image in payload.get("images", []):
        if not isinstance(image, dict):
            continue
        image_url = str(image.get("image_url", ""))
        media_type = str(image.get("media_type", "image/png"))
        if not image_url.startswith("data:image/"):
            continue
        image_blocks.append({
            "type": "input_image",
            "image_url": image_url,
            "media_type": media_type,
        })

    if image_blocks:
        goal = (
            f"{goal}\n\n"
            f"[web-ui-attached-images]\n"
            f"{json.dumps(image_blocks)}"
        )

    result = runtime_server._call(runtime_server._client.submit_task(
        goal,
        command_id="web-ui",
    ))
    return result.to_data()


@app.get("/stream/{task_id}")
async def stream(task_id: str):
    async def event_generator():
        assert runtime_server is not None
        cursor = 0
        while True:
            progress = runtime_server._call(
                runtime_server._client.get_task_progress(task_id, after=cursor)
            )
            events = progress.to_data().get("events", [])
            for event in events:
                cursor = max(cursor, event.get("sequence", cursor))
                yield f"data: {json.dumps(event)}\\n\\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def main() -> None:
    uvicorn.run(
        "tsm_agt.web.app:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )


if __name__ == "__main__":
    main()
