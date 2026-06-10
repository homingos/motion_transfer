# Motion Transfer API - Endpoints & Integration Guide

## Base URL
```
https://ai-team-flam-dev--motion-transfer-dev.modal.run
```
(Or `http://localhost:8000` if running locally)

## Endpoints Ready to Share

### 1. POST /generate
**Motion Transfer with Custom Video**

Generate motion transfer with a user-provided image and reference video.

```bash
curl -X POST https://ai-team-flam-dev--motion-transfer-dev.modal.run/generate \
  -F "image=@path/to/image.jpg" \
  -F "video=@path/to/video.mp4" \
  -F "prompt=happy walking"
```

**Parameters:**
- `image` (file, required): Subject image (PNG or JPG)
- `video` (file, optional): Reference motion video (defaults to bundled idle animation if omitted)
- `prompt` (string, optional): Text description of desired motion

**Response:**
```json
{
  "request_id": "a1b2c3d4e5f6",
  "status": "pending",
  "submitted_at": "2026-06-10T12:34:56Z",
  "image": "avatar.jpg",
  "video": "motion.mp4",
  "used_default_video": false,
  "prompt": "happy walking"
}
```

---

### 2. POST /idle-motion
**Generate Idle Motion from Avatar**

Create a natural idle/resting animation for an avatar. You only need the avatar's **_id** — the system automatically finds the avatar's face image and generates a subtle breathing/blinking animation.

**What It Does:**
1. Takes your avatar's _id (e.g., `6a22aa4a68059c63e7652347`)
2. Finds the avatar's face image in R2 cloud storage
3. Generates a natural idle motion (breathing, subtle head movement)
4. Saves the video back to R2
5. Updates the avatar's record with the animation link

**Simple Example:**

```bash
curl -X POST https://ai-team-flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "avatar_id=6a22aa4a68059c63e7652347"
```

**Request:**
- `avatar_id` (required): The MongoDB `_id` of your avatar (from `fableface.templates`)

**Response:**
```json
{
  "request_id": "a1b2c3d4e5f6",
  "status": "pending",
  "submitted_at": "2026-06-10T12:34:56Z",
  "avatar_id": "6a22aa4a68059c63e7652347",
  "video": "idle_avatar_15_reverse.mp4"
}
```

**How It Works (Behind the Scenes):**

Your avatar template doc in MongoDB looks like this:
```json
{
  "_id": "6a22aa4a68059c63e7652347",
  "name": "Ana",
  "source_assets": {
    "image_key": "templates/6a22aa4a68059c63e7652347/source",    // ← Avatar face image
    "idle_animation_key": null                                   // ← Gets filled with video
  },
  "status": "ready"
}
```

When you call `/idle-motion`:
1. ✅ System reads the `source_assets.image_key` to find the avatar's face image
2. ✅ Downloads the image from R2 cloud storage
3. ✅ Uses GPU to generate idle motion animation (~2-5 min)
4. ✅ Uploads the video back to R2
5. ✅ **Updates** your avatar doc:
   ```json
   {
     "source_assets": {
       "image_key": "templates/6a22aa4a68059c63e7652347/source",
       "idle_animation_key": "templates/6a22aa4a68059c63e7652347/idle"  // ← Now filled!
     },
     "status": "ready"
   }
   ```

**Status Updates:**
```
pending (0s)
  ↓
fetching_source (10-30s) — downloading avatar face from R2
  ↓
waiting_for_gpu → running (120-300s) — generating animation on GPU
  ↓
done — animation complete and saved
```

---

### 3. GET /idle-motion/{avatar_id}/preview
**Check Avatar Before Generating**

Quick validation — does this avatar exist and have a face image? No GPU cost, instant response.

```bash
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/idle-motion/6a22aa4a68059c63e7652347/preview
```

**Response if OK:**
```json
{
  "avatar_id": "6a22aa4a68059c63e7652347",
  "ok": true,
  "reason": null,
  "status": "ready",
  "image_key": "templates/6a22aa4a68059c63e7652347/source",
  "idle_animation_key": "templates/6a22aa4a68059c63e7652347/idle"
}
```

**Response if avatar missing or no image:**
```json
{
  "avatar_id": "6a22aa4a68059c63e7652347",
  "ok": false,
  "reason": "doc has no source_assets.image_key",
  "status": "ready"
}
```

---

### 4. GET /jobs/{request_id}
**Check Job Status**

Poll for job status (works for both /generate and /idle-motion).

```bash
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/a1b2c3d4e5f6
```

**Response:**
```json
{
  "request_id": "a1b2c3d4e5f6",
  "endpoint": "generate",
  "status": "running",
  "created_at": "2026-06-10T12:34:56Z",
  "updated_at": "2026-06-10T12:35:12Z",
  "image": "avatar.jpg",
  "video": "motion.mp4",
  "used_default_video": false,
  "prompt": "happy walking",
  "started_at": "2026-06-10T12:35:10Z"
}
```

**Status Values:**
- `pending` - Queued, waiting for processing
- `fetching_source` - (idle-motion only) Downloading source image from R2
- `waiting_for_gpu` - In queue for GPU
- `running` - Currently generating
- `done` - Complete; result ready at `/jobs/{request_id}/result`
- `failed` - Error occurred; check `error` field

---

### 5. GET /jobs/{request_id}/result
**Download Generated Video**

Download the generated motion transfer video (only available when `status == "done"`).

```bash
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/a1b2c3d4e5f6/result -o output.mp4
```

**Response:** Binary MP4 file

---

## Database Schema

### Collection: `motion_transfer_jobs` (in `fableface` DB)

Each request generates a document with this structure:

```json
{
  "_id": ObjectId,
  "request_id": "a1b2c3d4e5f6",
  "endpoint": "generate|idle-motion",
  "status": "pending|fetching_source|waiting_for_gpu|running|done|failed",
  "created_at": "2026-06-10T12:34:56Z",
  "updated_at": "2026-06-10T12:35:45Z",
  "submitted_at": "2026-06-10T12:34:56Z",
  "started_at": "2026-06-10T12:35:10Z",
  "finished_at": "2026-06-10T12:36:20Z",
  
  // For /generate endpoint
  "image": "filename.jpg",
  "video": "filename.mp4",
  "used_default_video": false,
  "prompt": "optional text prompt",
  
  // For /idle-motion endpoint
  "avatar_id": "507f1f77bcf86cd799439011",
  
  // On success
  "result": "outputs/api_a1b2c3d4e5f6.mp4",
  "animation_key": "templates/507f1f77bcf86cd799439011/idle",
  "link": "https://r2-public-url/...",
  
  // On failure
  "error": "error message or exception repr"
}
```

**Indexes to Create (recommended for queries):**
```javascript
db.motion_transfer_jobs.createIndex({ "request_id": 1 }, { unique: true })
db.motion_transfer_jobs.createIndex({ "endpoint": 1 })
db.motion_transfer_jobs.createIndex({ "status": 1 })
db.motion_transfer_jobs.createIndex({ "avatar_id": 1 })
db.motion_transfer_jobs.createIndex({ "created_at": -1 })
```

---

## Integration Notes

### Every Request Gets a Unique ID
- **request_id** is generated as `uuid.uuid4().hex[:12]` (12 hex chars)
- Stored immediately in MongoDB on request submission
- Even if status is already "ready", a new request_id is created
- Allows tracking duplicate requests and audit trails

### Persistent Storage
- All job metadata is persisted in MongoDB (previously in-memory)
- Survives server restarts
- Use `created_at` for request time and `finished_at` for completion time

### Polling Strategy
1. Submit request to `/generate` or `/idle-motion` → get `request_id`
2. Poll `GET /jobs/{request_id}` until `status == "done"` or `status == "failed"`
3. On success: `GET /jobs/{request_id}/result` to download MP4

### Error Handling
- Check `status == "failed"` and read `error` field for details
- HTTP 404 if request_id not found (typo or request expired)
- HTTP 409 if requesting result before status is "done"

---

## Environment Variables Required

```bash
MONGODB_URI=mongodb+srv://...
MONGODB_DB=fableface (default)
MONGODB_COLLECTION=templates (for templates doc)

R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=...
R2_PUBLIC_BASE_URL=https://... (optional; uses presigned URLs if unset)
```

---

## Example Integration Flow

### Generate Idle Motion for Avatar

```python
import requests
import time

BASE = "https://ai-team-flam-dev--motion-transfer-dev.modal.run"

# Step 1: Start idle motion generation
avatar_id = "6a22aa4a68059c63e7652347"  # Ana avatar
resp = requests.post(f"{BASE}/idle-motion", data={"avatar_id": avatar_id})
request_id = resp.json()["request_id"]
print(f"Started: {request_id}")

# Step 2: Poll until done (checks every 5 seconds)
while True:
    job = requests.get(f"{BASE}/jobs/{request_id}").json()
    print(f"Status: {job['status']}")
    
    if job["status"] == "done":
        # Step 3: Download the video
        video = requests.get(f"{BASE}/jobs/{request_id}/result")
        with open("idle_animation.mp4", "wb") as f:
            f.write(video.content)
        print(f"✅ Done! Avatar updated in MongoDB")
        print(f"   Saved to: idle_animation.mp4")
        break
    elif job["status"] == "failed":
        print(f"❌ Failed: {job['error']}")
        break
    
    time.sleep(5)  # Poll every 5 seconds
```

### Check Avatar Before Generating

```python
import requests

BASE = "https://ai-team-flam-dev--motion-transfer-dev.modal.run"
avatar_id = "6a22aa4a68059c63e7652347"

# Quick validation (instant, no GPU)
resp = requests.get(f"{BASE}/idle-motion/{avatar_id}/preview").json()

if resp["ok"]:
    print(f"✅ Avatar ready: {avatar_id}")
    print(f"   Image: {resp['image_key']}")
    # Safe to call /idle-motion now
else:
    print(f"❌ Avatar issue: {resp['reason']}")
    # Don't call /idle-motion, avatar is missing or has no image
```
