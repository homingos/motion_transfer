# Motion Transfer Deployment Guide

## Overview

This guide explains the deployment setup for the Motion Transfer application across two Modal environments: **dev** (R2-only) and **feat** (full mode).

## Environment Configurations

### Dev Environment
- **Endpoint**: https://ai-team-flam-dev--motion-transfer-dev.modal.run
- **API Mode**: `r2_only` (image upload disabled, avatar_id only)
- **File**: `modal_app_dev.py`
- **Features**:
  - `/idle-motion` - Generate idle motion for avatar_id
  - `/idle-motion/{avatar_id}/preview` - Dry-run validation
  - `/animate` - Unified endpoint (avatar_id mode only)
  - `/avatar/{avatar_id}/info` - Query avatar metadata
  - `/avatar/{avatar_id}/status` - Update avatar status

### Feat Environment
- **Endpoint**: https://ai-team-flam-feat--motion-transfer-feat.modal.run
- **API Mode**: `full` (both image upload and avatar_id)
- **File**: `modal_app_feat.py`
- **Features**:
  - `/generate` - Direct image upload mode
  - `/idle-motion` - Generate idle motion for avatar_id
  - `/idle-motion/{avatar_id}/preview` - Dry-run validation
  - `/animate` - Unified endpoint (image and avatar_id modes)
  - `/avatar/{avatar_id}/info` - Query avatar metadata
  - `/avatar/{avatar_id}/status` - Update avatar status

## New Endpoints

### GET /avatar/{avatar_id}/info
Query avatar metadata without generating.

**Response**:
```json
{
  "avatar_id": "507f1f77bcf86cd799439011",
  "status": "ready",
  "image_key": "avatars/123/source.jpg",
  "image_exists_in_r2": true,
  "idle_animation_key": "templates/507f1f77bcf86cd799439011/idle",
  "idle_animation_exists_in_r2": true
}
```

### POST /avatar/{avatar_id}/status
Update avatar status in MongoDB.

**Parameters**:
- `avatar_id` (path) - Avatar ID
- `status` (form) - One of: `processing`, `ready`, `failed`
- `failure_reason` (form, optional) - Error message if status=failed

**Response**:
```json
{
  "avatar_id": "507f1f77bcf86cd799439011",
  "status": "ready",
  "updated": true
}
```

## Deployment Steps

### Prerequisites
- Modal CLI installed
- Credentials configured (see .modal.toml)
- Model weights uploaded to volumes (if first deployment)

### Deploy Dev Environment

```bash
# Create dev environment (once)
modal environment create dev

# Create volume and upload models (once)
modal volume create motion-transfer-models -e dev
modal volume put motion-transfer-models ./models/distilled /distilled -e dev
modal volume put motion-transfer-models ./models/gemma /gemma -e dev
modal volume put motion-transfer-models ./models/ic-lora /ic-lora -e dev
modal volume put motion-transfer-models ./models/upscaler /upscaler -e dev

# Deploy the app
modal deploy modal_app_dev.py -e dev
```

### Deploy Feat Environment

```bash
# Create feat environment (once)
modal environment create feat

# Deploy the app (reuse dev volume if in same workspace)
modal deploy modal_app_feat.py -e feat
```

## Testing

### Local Testing
1. **Dev Server UI Test** (no models):
   ```bash
   python dev_server.py
   # Open http://localhost:8000
   ```

2. **Full Server Test** (requires models):
   ```bash
   python server.py
   # Open http://localhost:8000
   ```

### Remote Testing (via cURL)

#### Test Avatar Info Endpoint
```bash
# Dev
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/avatar/{avatar_id}/info

# Feat
curl https://ai-team-flam-feat--motion-transfer-feat.modal.run/avatar/{avatar_id}/info
```

#### Test Image Upload (Feat Only)
```bash
curl -X POST \
  -F "image=@/path/to/image.jpg" \
  https://ai-team-flam-feat--motion-transfer-feat.modal.run/animate \
  --output result.mp4
```

#### Test Avatar ID Mode
```bash
# Submit job
REQUEST_ID=$(curl -X POST \
  -F "avatar_id=507f1f77bcf86cd799439011" \
  https://ai-team-flam-dev--motion-transfer-dev.modal.run/idle-motion \
  | jq -r '.request_id')

# Check status
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/${REQUEST_ID}

# Download result when done
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/${REQUEST_ID}/result \
  --output result.mp4
```

## API Mode Logic

The `API_MODE` environment variable controls endpoint behavior:

- **`full`** (default, feat): Both `/generate` and `/animate` allow image uploads
- **`r2_only`** (dev): Image uploads rejected; only avatar_id mode allowed

This is enforced in `server.py`:
```python
API_MODE = os.environ.get("API_MODE", "full")

# In /animate endpoint:
if API_MODE == "r2_only" and image:
    raise HTTPException(400, "image upload not allowed in this environment")
```

## Key Files

- `modal_app_dev.py` - Dev environment configuration
- `modal_app_feat.py` - Feat environment configuration
- `server.py` - FastAPI server with all endpoints
- `integrations.py` - MongoDB and R2 integration
- `pipeline_runtime.py` - Core generation pipeline
- `modal_common.py` - Shared Modal configuration

## Environment Variables (via Modal Secrets)

- `MONGODB_URI` - MongoDB connection string
- `R2_ACCOUNT_ID` - Cloudflare R2 account ID
- `R2_ACCESS_KEY_ID` - R2 access key
- `R2_SECRET_ACCESS_KEY` - R2 secret key
- `R2_BUCKET_NAME` - R2 bucket name
- `R2_PUBLIC_BASE_URL` (optional) - Public URL for R2 objects

## Monitoring

Monitor logs and metrics:
```bash
modal logs modal_app_dev -e dev
modal logs modal_app_feat -e feat
```

Check resource usage:
```bash
modal volume ls -e dev
modal volume ls -e feat
```
