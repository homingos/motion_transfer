# FLAM Motion Transfer - Dev Environment Usage Guide

## Dev URL

```
https://flam-dev--motion-transfer-dev.modal.run
```

## Accessing the Application

### Web UI
Open in browser:
```
https://flam-dev--motion-transfer-dev.modal.run/
```

Upload an avatar image and select gender to generate idle motion video.

---

## API Endpoints

### 1. **POST /generate** - Generate from uploaded image & reference video

**Parameters:**
- `image` (File, required) - Subject image (PNG/JPG)
- `video` (File, optional) - Custom reference motion video (if omitted, uses gender-based default)
- `gender` (Form, required) - Avatar gender: "male" or "female"
- `prompt` (Form, optional) - Text description of motion
- `target_output_seconds` (Form, optional) - Output duration (default 2.0s on dev)

**Example:**
```bash
curl -X POST https://flam-dev--motion-transfer-dev.modal.run/generate \
  -F "image=@your_image.jpg" \
  -F "gender=male"
```

**Response:**
```json
{
  "request_id": "abc123def456",
  "status": "pending",
  "image": "your_image.jpg",
  "video": "man.mp4",
  "submitted_at": "2026-06-20T15:00:00+00:00"
}
```

---

### 2. **POST /idle-motion** - Generate from GCS image URL

**Parameters:**
- `image_url` (Form, required) - GCS/URL to avatar image
- `gender` (Form, required) - Avatar gender: "male" or "female"

**Example:**
```bash
curl -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/.../image.jpg" \
  -F "gender=male"
```

**Response:**
```json
{
  "request_id": "2d191f4269e7",
  "status": "pending",
  "image_url": "https://storage.googleapis.com/.../image.jpg",
  "video": "man.mp4",
  "submitted_at": "2026-06-20T15:47:17+00:00"
}
```

---

### 3. **GET /jobs/{request_id}** - Check job status

**Example:**
```bash
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/2d191f4269e7
```

**Response (Pending):**
```json
{
  "status": "pending",
  "image_url": "...",
  "video": "man.mp4",
  "submitted_at": "2026-06-20T15:47:17+00:00"
}
```

**Response (Running):**
```json
{
  "status": "running",
  "image_url": "...",
  "video": "man.mp4",
  "submitted_at": "2026-06-20T15:47:17+00:00",
  "started_at": "2026-06-20T15:47:21+00:00"
}
```

**Response (Done):**
```json
{
  "status": "done",
  "image_url": "...",
  "video": "man.mp4",
  "submitted_at": "2026-06-20T15:47:17+00:00",
  "started_at": "2026-06-20T15:47:21+00:00",
  "finished_at": "2026-06-20T15:47:56+00:00",
  "animation_key": "https://storage.googleapis.com/...",
  "link": "https://storage.googleapis.com/..."
}
```

---

### 4. **GET /jobs/{request_id}/result** - Download output video

**Example:**
```bash
curl -O https://flam-dev--motion-transfer-dev.modal.run/jobs/2d191f4269e7/result
```

Downloads the generated MP4 file.

---

### 5. **GET /reference/{name}** - Serve reference videos

**Available reference videos:**
- `man.mp4` - Male default motion
- `women.mp4` - Female motion
- `male.mp4` - Male motion (alternative)
- `default_1.mp4` - Default motion
- `10sec_trimmed.mp4` - Trimmed reference
- `4sec-loop` - 4-second looped reference

**Example:**
```bash
curl -O https://flam-dev--motion-transfer-dev.modal.run/reference/man.mp4
```

---

## Job Status Values

### Status Lifecycle

```
pending
   ↓
fetching_source (only for /idle-motion and /animate avatar_id mode)
   ↓
waiting_for_gpu → (retry loop on transient GPU errors)
   ↓
running → (35-45 seconds, includes generation + reverse-append)
   ↓
done (✅ Success) or failed (❌ Error/Timeout)
```

### Detailed Status Descriptions

| Status | When Set | What's Happening | Duration | Next State |
|--------|----------|------------------|----------|-----------|
| **pending** | Immediately after job submission (POST request received) | Job ID created, saved to database, background thread starting | < 1 second | `fetching_source` (if URL endpoint) or `waiting_for_gpu` (if file endpoint) |
| **fetching_source** | Only on `/idle-motion` and `/animate` avatar_id mode, before image download | System downloading image from GCS URL using requests library; retries on 5xx errors | 1-5 seconds | `waiting_for_gpu` (on success) or `failed` (on persistent download errors) |
| **waiting_for_gpu** | Before acquiring GPU lock (`_GPU_LOCK`); also set again on transient GPU errors with exponential backoff | GPU unavailable (container is busy processing another job, or device allocation pending); retries up to 30 minutes with 2s→30s backoff | Seconds to minutes (depends on queue) | `running` (on GPU acquired) or `failed` (on 30-min timeout) |
| **running** | After GPU lock acquired, just before `pipeline_runtime.generate()` call | Model inference in progress: image encoding, motion conditioning, latent generation, video decoding, reverse-append, optional upload to GCS | ~35-45 seconds | `done` (on success) or `failed` (on any error) |
| **done** | After generation, reverse-append, and (if applicable) GCS upload complete | ✅ Video successfully generated and available for download; result URL/path saved | N/A | Terminal state |
| **failed** | On any non-transient error (validation, file I/O, model error, encoding error) or GPU timeout after 30 minutes | ❌ Generation could not complete; error message saved; for `/idle-motion`, MongoDB status also updated to `failed` | N/A | Terminal state |

### Transient vs. Persistent Errors

**Transient errors (retried indefinitely up to 30 min):**
- `CUDA out of memory`
- GPU allocation failures (device not ready)
- Temporary GPU errors

**Persistent errors (fail immediately):**
- Invalid image format
- Missing source files
- Network errors downloading from GCS (after retries)
- Model inference errors
- Output file write failures

### Polling Strategy

Poll every 3-5 seconds. Stop polling when status is `done` or `failed`:

```bash
while true; do
  STATUS=$(curl -s https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID | jq -r '.status')
  
  if [ "$STATUS" = "done" ]; then
    echo "✅ Completed!"
    break
  elif [ "$STATUS" = "failed" ]; then
    ERROR=$(curl -s https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID | jq -r '.error')
    echo "❌ Failed: $ERROR"
    break
  fi
  
  echo "Status: $STATUS"
  sleep 5
done
```

---

## Complete Workflow Example

### Step 1: Submit Job
```bash
REQUEST_ID=$(curl -s -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/bucket-fi-production-apps-0672ab2d/original/images/owaibstq8bfds8zflbok20qm.png" \
  -F "gender=male" | jq -r '.request_id')

echo "Job ID: $REQUEST_ID"
```

### Step 2: Poll for Completion
```bash
while true; do
  STATUS=$(curl -s https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID | jq -r '.status')
  echo "Status: $STATUS"
  
  if [ "$STATUS" = "done" ]; then
    echo "✅ Job completed!"
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "❌ Job failed!"
    break
  fi
  
  sleep 5
done
```

### Step 3: Download Result
```bash
curl -O https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID/result
```

---

## Default Settings (Dev Environment)

| Setting | Value |
|---------|-------|
| **Output duration** | 2.0 seconds per side (4.0s total with reverse) |
| **Frame rate** | 25 FPS |
| **Resolution** | 768x768 |
| **Gender parameter** | Required (male → man.mp4, female → women.mp4) |
| **Generation time** | ~35-45 seconds |
| **Final video duration** | ~3.92 seconds (2s + 2s reversed, VAE cropped) |

---

## Testing Quick Links

### Test with Different Images
```bash
# Test 1
curl -s -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/bucket-fi-production-apps-0672ab2d/original/images/owaibstq8bfds8zflbok20qm.png" \
  -F "gender=male"

# Test 2
curl -s -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/prj-d-fi-bkt-flam-ai-team-0/flam-avs/faceshot/motion-transfer/IMG_6577%20(1).jpg" \
  -F "gender=male"
```

---

## Notes

- **Gender is required** on both `/generate` and `/idle-motion` endpoints
  - `gender=male` → uses `man.mp4` reference
  - `gender=female` → uses `women.mp4` reference
- **Video output is ~4 seconds** (2 seconds generated + 2 seconds reversed for natural looping)
- **VAE frame cropping** reduces duration slightly from exactly 4s to 3.92s (expected behavior)
- **Polling interval**: Check status every 3-5 seconds until done
- **Max concurrent jobs**: 1 per GPU container (scales to 5 containers)
