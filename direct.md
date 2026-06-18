# Direct Mode (Feat Environment)

## Overview
This is the **image upload mode** - you send an image file directly and get back the MP4 video immediately. No waiting, no polling.

**URL**: `https://flam-feat--motion-transfer-feat.modal.run`

## How It Works (Plain Terms)

1. You upload an image (JPG or PNG)
2. System processes it immediately
3. Returns the generated MP4 video directly
4. No need to check status, just download

## API Endpoints

### 1. Generate from Image (Fast)
**Upload image and get MP4 directly (takes 2-5 minutes)**

```bash
curl -X POST https://flam-feat--motion-transfer-feat.modal.run/animate \
  -F "image=@my_photo.jpg" \
  --output result.mp4
```

That's it! Your video is saved as `result.mp4`

### 2. Generate with Avatar ID (Async - like Dev mode)
**Submit job and poll for status**

```bash
curl -X POST https://flam-feat--motion-transfer-feat.modal.run/idle-motion \
  -F "avatar_id=507f1f77bcf86cd799439011"
```

Response:
```json
{
  "request_id": "abc123def456",
  "status": "pending"
}
```

Then check status like in R2 mode (see polling section below)

### 3. Check Job Status
**For avatar ID jobs, check progress**

```bash
curl https://flam-feat--motion-transfer-feat.modal.run/jobs/abc123def456
```

### 4. Download Result
**Download video from avatar ID job**

```bash
curl https://flam-feat--motion-transfer-feat.modal.run/jobs/abc123def456/result \
  --output result.mp4
```

### 5. Check Avatar Info
**Get avatar details**

```bash
curl https://flam-feat--motion-transfer-feat.modal.run/avatar/507f1f77bcf86cd799439011/info
```

## Step-by-Step Examples

### Quick Method (Image Upload)
```bash
# One command - upload image, get video back
curl -X POST https://flam-feat--motion-transfer-feat.modal.run/animate \
  -F "image=@my_photo.jpg" \
  --output my_video.mp4

# Wait 2-5 minutes... Done!
```

### Async Method (Avatar ID)
```bash
# 1. Submit job
REQUEST_ID=$(curl -X POST https://flam-feat--motion-transfer-feat.modal.run/idle-motion \
  -F "avatar_id=507f1f77bcf86cd799439011" | jq -r '.request_id')

echo "Job ID: $REQUEST_ID"

# 2. Check status every 10 seconds
while true; do
  STATUS=$(curl https://flam-feat--motion-transfer-feat.modal.run/jobs/$REQUEST_ID | jq -r '.status')
  echo "Status: $STATUS"
  if [ "$STATUS" = "done" ]; then
    break
  fi
  sleep 10
done

# 3. Download
curl https://flam-feat--motion-transfer-feat.modal.run/jobs/$REQUEST_ID/result \
  --output my_video.mp4
```

## Parameters

### Image Upload (`/animate`)
- **image** (required) - Your image file (JPG or PNG)
- **prompt** (optional) - Text description of motion
- **lora_strength** (optional) - Style control (0.0-1.0, default 0.8)
- **video_strength** (optional) - Motion intensity (0.0-1.0, default 0.85)

Example with all options:
```bash
curl -X POST https://flam-feat--motion-transfer-feat.modal.run/animate \
  -F "image=@photo.jpg" \
  -F "prompt=happy, dancing" \
  -F "lora_strength=0.7" \
  -F "video_strength=0.9" \
  --output video.mp4
```

### Avatar ID Mode (`/idle-motion`)
- **avatar_id** (required) - Avatar ID from database
- **lora_strength** (optional) - Style control (0.0-1.0, default 0.8)
- **video_strength** (optional) - Motion intensity (0.0-1.0, default 0.85)

## Key Differences vs R2 Mode

| Feature | Direct Mode | R2 Mode |
|---------|------------|---------|
| Image Upload | ✅ Yes (immediate) | ❌ No |
| Avatar ID Mode | ✅ Yes (async) | ✅ Yes (async) |
| Result Speed | 2-5 min (direct) | 2-5 min (async) |
| Best For | Quick testing | Production with avatars |

## Notes

- Image upload returns MP4 directly (no polling needed)
- Avatar ID mode works the same as R2 mode
- Images must be JPG or PNG
- Maximum file size depends on your network
- Processing takes 2-5 minutes regardless of method
