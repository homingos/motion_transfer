# GCS Storage Mode (Modal Deployment on FLAM)

## Overview

This is the **GCS-backed mode** with FLAM Resource API integration. You send a GCS image URL, the system generates motion video with forward + reverse loop, and uploads it to GCS as a permanent public resource.

## Environment URLs

### Production
- **Main (Production)**: `https://flam--motion-transfer.modal.run`

### Staging & Development
- **Dev**: `https://flam-dev--motion-transfer-dev.modal.run`
- **Feat**: `https://flam-feat--motion-transfer-feat.modal.run`

## How It Works (Plain Terms)

1. You provide a **GCS image URL** (direct public URL to the image)
2. System downloads the image from GCS
3. Generates the motion video (default 5 seconds)
4. **Appends reversed clip** (5s forward + 5s reverse = natural loop, ~10s total)
5. Uploads the looped video to GCS via **FLAM Resource API** (internal service)
6. Returns permanent public GCS URL in response

## Storage Details

- **Input**: Public GCS image URLs (downloaded via HTTP, no auth required)
- **Output**: Uploaded to GCS via FLAM Resource API (internal authenticated service)
- **Result URLs**: Permanent public `https://storage.googleapis.com/...` URLs
- **Retention**: Videos stored indefinitely in GCS

## API Endpoints

### 1. Generate Motion Video
**Submit a generation job with GCS image URL**

```bash
curl -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/bucket/path/to/image.jpg"
```

Response:
```json
{
  "request_id": "abc123def456",
  "status": "pending",
  "image_url": "https://storage.googleapis.com/bucket/path/to/image.jpg",
  "submitted_at": "2024-06-18T14:30:00Z"
}
```

### 2. Check Job Status
**Check if your generation is done**

```bash
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/abc123def456
```

Response (while running):
```json
{
  "status": "running",
  "image_url": "https://storage.googleapis.com/bucket/path/to/image.jpg",
  "started_at": "2024-06-18T14:30:05Z"
}
```

Response (when done):
```json
{
  "status": "done",
  "finished_at": "2024-06-18T14:35:45Z",
  "animation_key": "https://storage.googleapis.com/bucket/original/videos/xyz123.mp4",
  "link": "https://storage.googleapis.com/bucket/original/videos/xyz123.mp4"
}
```

### 3. Download Result
**Get your generated MP4 video**

```bash
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/abc123def456/result \
  --output result.mp4
```

## Step-by-Step Example

```bash
# 1. Submit generation job with GCS image URL
REQUEST_ID=$(curl -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/bucket/path/to/image.jpg" | jq -r '.request_id')

echo "Submitted job: $REQUEST_ID"

# 2. Wait and check status (keep polling)
for i in {1..120}; do
  STATUS=$(curl -s https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID | jq -r '.status')
  echo "Status: $STATUS"
  if [ "$STATUS" = "done" ]; then
    echo "Generation complete!"
    break
  fi
  sleep 5
done

# 3. Download result
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID/result \
  --output my_video.mp4

echo "Done! Video saved as my_video.mp4 (forward + reverse loop, ~10s)"
```

## Parameters

### Generate Motion Video (`/idle-motion`)
- **image_url** (required) - Direct GCS URL to the image (must be publicly accessible)
- **lora_strength** (optional) - LoRA strength control (0.0-1.0, default 0.8)
- **video_strength** (optional) - Motion conditioning strength (0.0-1.0, default 0.95)
- **reference** (optional) - Preset reference video: "default" (female), "female", or "male" (default: "default")

## How to Check Jobs

### Poll for Job Status
```bash
# Keep checking until job is done
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/abc123def456
```

This will return status: `pending`, `running`, `done`, or `failed`

### Automated Polling Script
```bash
#!/bin/bash
REQUEST_ID="abc123def456"
ENDPOINT="https://flam-dev--motion-transfer-dev.modal.run"

while true; do
  RESPONSE=$(curl -s $ENDPOINT/jobs/$REQUEST_ID)
  STATUS=$(echo $RESPONSE | jq -r '.status')
  echo "Status: $STATUS | Time: $(date)"
  
  if [ "$STATUS" = "done" ]; then
    echo "Job completed successfully!"
    GCS_URL=$(echo $RESPONSE | jq -r '.link')
    echo "GCS URL: $GCS_URL"
    echo "Video duration: ~10s (5s forward + 5s reverse loop)"
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "Job failed!"
    echo "Error: $RESPONSE" | jq '.'
    break
  fi
  sleep 10  # Check every 10 seconds
done
```

## Testing with Real GCS URLs

To test the full flow:

```bash
# 1. Submit with a real GCS image URL
curl -X POST https://flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "image_url=https://storage.googleapis.com/bucket-fi-production-apps-0672ab2d/original/images/abjc7aansjubgqxv5epzew7g.jpg"

# Response includes request_id
# Save this REQUEST_ID

# 2. Immediately check status (should be pending or fetching_source)
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/REQUEST_ID

# 3. Wait 10 seconds and check again (should be running)
sleep 10
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/REQUEST_ID

# 4. After processing (2-5 mins), check final status (should be done)
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/REQUEST_ID

# 5. If status is done, download the result (forward + reverse looped video)
curl https://flam-dev--motion-transfer-dev.modal.run/jobs/REQUEST_ID/result \
  --output test-result.mp4
```

## Key Features

- ✅ **GCS Input URLs**: Public GCS image URLs with direct download support
- ✅ **GCS Output Storage**: Videos uploaded to GCS via FLAM Resource API as permanent public resources
- ✅ **Natural Looping**: Forward generation (5s) + reversed clip (5s) = ~10s loopable video
- ✅ **Fast Processing**: ~20-40 seconds per generation
- ✅ **Multiple Reference Styles**: Default (female), female, and male motion references
- ✅ **Permanent URLs**: GCS public URLs never expire

## Status Transitions

```
pending
  ↓
fetching_source  (downloading GCS image)
  ↓
waiting_for_gpu  (queued for GPU)
  ↓
running  (generating motion + appending reverse + uploading to GCS)
  ↓
done  (returns GCS public URL)
```

## Notes

- Image must be accessible from a public GCS URL (no authentication required for download)
- Generation takes 2-5 minutes (varies by queue)
- Videos are looped: forward motion + reverse motion (natural seamless loop)
- Result videos are stored in GCS permanently with public HTTP access
- FLAM Resource API handles all GCS authentication server-side
