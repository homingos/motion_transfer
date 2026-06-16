#!/bin/bash
# API testing script for Motion Transfer endpoints

set -e

# Configuration
BASE_URL="${1:-http://localhost:8000}"
AVATAR_ID="${2:-507f1f77bcf86cd799439011}"

echo "=================================="
echo "Motion Transfer API Tests"
echo "=================================="
echo "Base URL: $BASE_URL"
echo "Avatar ID: $AVATAR_ID"
echo ""

# Test 1: Avatar Info Endpoint
echo "1. Testing GET /avatar/{avatar_id}/info"
echo "   curl $BASE_URL/avatar/$AVATAR_ID/info"
curl -s -X GET "$BASE_URL/avatar/$AVATAR_ID/info" | python3 -m json.tool || echo "  ✗ Failed (might need valid avatar_id or MongoDB connection)"
echo ""

# Test 2: Avatar Status Update Endpoint
echo "2. Testing POST /avatar/{avatar_id}/status"
echo "   curl -X POST $BASE_URL/avatar/$AVATAR_ID/status -d status=ready"
curl -s -X POST "$BASE_URL/avatar/$AVATAR_ID/status" \
  -F "status=ready" \
  -F "failure_reason=" | python3 -m json.tool || echo "  ✗ Failed"
echo ""

# Test 3: Idle Motion Preview
echo "3. Testing GET /idle-motion/{avatar_id}/preview"
echo "   curl $BASE_URL/idle-motion/$AVATAR_ID/preview"
curl -s -X GET "$BASE_URL/idle-motion/$AVATAR_ID/preview" | python3 -m json.tool || echo "  ✗ Failed"
echo ""

# Test 4: List available routes
echo "4. Testing API availability"
echo "   curl $BASE_URL/docs (Swagger UI)"
curl -s -I "$BASE_URL/docs" | head -1
echo ""

echo "=================================="
echo "API Tests Complete"
echo "=================================="
echo ""
echo "Next steps:"
echo "1. For full testing, deploy to Modal:"
echo "   modal deploy modal_app_dev.py -e dev"
echo "   modal deploy modal_app_feat.py -e feat"
echo ""
echo "2. Then test against deployed endpoints:"
echo "   bash test_api.sh https://ai-team-flam-dev--motion-transfer-dev.modal.run"
echo "   bash test_api.sh https://ai-team-flam-feat--motion-transfer-feat.modal.run"
