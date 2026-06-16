# Motion Transfer Changes Summary

## Overview
Enhanced the Motion Transfer application with new avatar metadata management endpoints for both **dev** (R2-only) and **feat** (full mode) Modal environments.

## Changes Made

### 1. New API Endpoints (server.py)

#### GET `/avatar/{avatar_id}/info`
- **Purpose**: Query avatar metadata without generating
- **Returns**: Avatar status, image_key, idle_animation_key, and their R2 existence status
- **Use Case**: Check if an avatar is ready before requesting generation
- **Available on**: Both dev and feat

#### POST `/avatar/{avatar_id}/status`
- **Purpose**: Update avatar status in MongoDB
- **Parameters**: avatar_id, status (processing/ready/failed), optional failure_reason
- **Use Case**: Manage avatar workflow state manually or on error recovery
- **Available on**: Both dev and feat

### 2. Code Improvements

#### Logging Setup
- Added `import logging` and logger instance to server.py
- All new endpoints use proper logging for debugging

#### API Mode Logic
- Maintained existing `API_MODE` environment variable system
- Dev deployment: `API_MODE="r2_only"` prevents direct image uploads
- Feat deployment: `API_MODE="full"` (default) allows both image and avatar_id modes

#### Function Signature Enhancements
- Updated `run_job()` to support `lora_strength` and `video_strength` parameters
- Updated `run_idle_job()` to support optional video/LoRA parameters
- Refactored `_set()` → `_update_job()` with safe dict access

### 3. Documentation

#### DEPLOYMENT_GUIDE.md
- Complete setup instructions for dev and feat environments
- Endpoint documentation with examples
- Testing procedures (local and remote)
- Environment variable reference

#### Test Scripts
- `test_api.sh` - Shell script for testing endpoints via cURL
- `test_syntax.py` - AST-based syntax validation
- `test_endpoints.py` - Comprehensive module validation (requires dependencies)

## Branch Status

### Dev Branch
- ✅ Server endpoints with r2_only mode enabled
- ✅ Avatar metadata query/management endpoints
- ✅ All documentation and tests
- Latest commit: `e9b5657` (Merged feat changes)

### Feat Branch
- ✅ Server endpoints with full mode (default)
- ✅ Avatar metadata query/management endpoints
- ✅ All documentation and tests
- Latest commit: `e9b5657` (Test and deployment docs)

### Main Branch
- 🔄 Behind dev/feat (intentional - main is production)

## API Endpoint Summary

### Both Environments
```
GET  /                              # Serve UI
GET  /avatar/{avatar_id}/info      # Query avatar metadata
POST /avatar/{avatar_id}/status    # Update avatar status
POST /idle-motion                  # Async avatar generation
GET  /idle-motion/{avatar_id}/preview  # Dry-run validation
POST /animate                      # Unified endpoint
GET  /jobs/{request_id}            # Poll job status
GET  /jobs/{request_id}/result     # Download result
```

### Dev Only (r2_only mode)
```
POST /animate                      # avatar_id mode only
POST /idle-motion                  # Avatar ID required
```

### Feat Only (full mode)
```
POST /generate                     # Direct image upload
POST /animate                      # Both image and avatar_id modes
```

## Testing

### Quick Local Test
```bash
# Test syntax
python test_syntax.py

# Test API endpoints (requires server running)
bash test_api.sh http://localhost:8000
```

### Deployment Testing
```bash
# Deploy to Modal
modal deploy modal_app_dev.py -e dev
modal deploy modal_app_feat.py -e feat

# Test endpoints
bash test_api.sh https://ai-team-flam-dev--motion-transfer-dev.modal.run
bash test_api.sh https://ai-team-flam-feat--motion-transfer-feat.modal.run
```

## Example Usage

### Query Avatar Metadata
```bash
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/avatar/507f1f77bcf86cd799439011/info
```

Response:
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

### Update Avatar Status
```bash
curl -X POST https://ai-team-flam-dev--motion-transfer-dev.modal.run/avatar/507f1f77bcf86cd799439011/status \
  -F "status=processing"
```

### Generate with Image (Feat Only)
```bash
curl -X POST https://ai-team-flam-feat--motion-transfer-feat.modal.run/animate \
  -F "image=@image.jpg" \
  --output result.mp4
```

### Generate with Avatar ID (Both)
```bash
# Submit job
REQUEST_ID=$(curl -X POST https://ai-team-flam-dev--motion-transfer-dev.modal.run/idle-motion \
  -F "avatar_id=507f1f77bcf86cd799439011" \
  | jq -r '.request_id')

# Poll status
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID

# Download when ready
curl https://ai-team-flam-dev--motion-transfer-dev.modal.run/jobs/$REQUEST_ID/result \
  --output result.mp4
```

## Implementation Details

### Avatar Metadata Flow
1. User calls `/avatar/{avatar_id}/info`
2. Server queries MongoDB for template doc
3. Returns status and asset keys
4. Checks R2 existence for each key
5. Returns comprehensive metadata

### Status Update Flow
1. User calls `/avatar/{avatar_id}/status`
2. Server validates status value
3. Updates MongoDB with new status and timestamp
4. Best-effort logging (doesn't fail if DB is unavailable)

### API Mode Enforcement
- Dev sets `API_MODE="r2_only"` in `modal_app_dev.py`
- Feat leaves default `API_MODE="full"` in `modal_app_feat.py`
- `/animate` endpoint checks mode in `server.py:322-323`
- Image uploads rejected in r2_only mode

## Files Modified

### Core Changes
- `server.py` - New endpoints, logging, API mode logic

### Configuration
- `modal_app_dev.py` - Sets API_MODE environment variable
- `modal_app_feat.py` - Default full mode

### Documentation & Tests
- `DEPLOYMENT_GUIDE.md` - Complete deployment guide
- `test_api.sh` - API testing script
- `test_syntax.py` - Syntax validation
- `test_endpoints.py` - Module validation

## Next Steps

1. **Test Locally**
   ```bash
   python test_syntax.py
   python dev_server.py  # UI test
   ```

2. **Deploy to Modal**
   ```bash
   modal deploy modal_app_dev.py -e dev
   modal deploy modal_app_feat.py -e feat
   ```

3. **Verify Deployment**
   ```bash
   modal logs modal_app_dev -e dev
   modal logs modal_app_feat -e feat
   ```

4. **Test Remote Endpoints**
   ```bash
   bash test_api.sh https://ai-team-flam-dev--motion-transfer-dev.modal.run
   bash test_api.sh https://ai-team-flam-feat--motion-transfer-feat.modal.run
   ```

## Backward Compatibility

✅ **All changes are backward compatible**
- New endpoints don't affect existing flows
- API mode logic preserves existing restrictions
- Function signature updates are additive (optional parameters)

## Notes

- Both branches (dev/feat) are now in sync with all changes
- Main branch remains unchanged (production-ready)
- Environment-specific behavior is controlled via Modal app configuration
- All documentation is included in the repository
