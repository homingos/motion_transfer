"""MongoDB status tracking + FLAM Resource API for idle animation uploads.

Credentials come from environment variables (Modal secrets — never hardcode):
  MONGODB_URI

Uploads via FLAM Resource API (internal GCS-backed service):
  POST /api/v1/resources -> get signed_url + resource_url
  PUT <signed_url> -> upload file
  Returns permanent public resource_url

Mongo target: DB ``fableface`` (override MONGODB_DB), collection ``templates``
(override MONGODB_COLLECTION). Docs are keyed by ObjectId ``_id``. We read:
  source_assets.image_key:        the GCS URL of the subject image to animate (input)
and update:
  status:                "processing" | "ready" | "failed"
  source_assets.idle_animation_key:  the resource_url of the generated idle video
                                     (on success; written before status->ready)
  failure_reason:        the error string (on failure), else None
(idle_vector_key is a separate field and is left untouched.)

pymongo is imported lazily so importing this module stays cheap and CUDA-free
(safe for the Modal memory-snapshot path).
"""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MONGO_DB = os.environ.get("MONGODB_DB", "fableface")
MONGO_COLLECTION = os.environ.get("MONGODB_COLLECTION", "templates")

_mongo_lock = threading.Lock()
_mongo_coll = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collection():
    """Lazily create a pooled MongoClient and return the target collection."""
    global _mongo_coll
    if _mongo_coll is None:
        with _mongo_lock:
            if _mongo_coll is None:
                from pymongo import MongoClient

                uri = os.environ["MONGODB_URI"]
                client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
                _mongo_coll = client[MONGO_DB][MONGO_COLLECTION]
    return _mongo_coll


def _doc_filter(avatar_id: str) -> dict:
    """Match the template doc by ObjectId _id (fall back to raw value if not an ObjectId)."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        return {"_id": ObjectId(avatar_id)}
    except (InvalidId, TypeError):
        return {"_id": avatar_id}


def set_status(avatar_id: str, status: str, idle_vector_link: str | None = None,
               failure_reason: str | None = None) -> None:
    """Update the template doc's status (+ idle_vector_key / failure_reason).

    Best-effort: a DB hiccup is logged, never raised, so it can't crash the job.
    """
    fields: dict = {"status": status, "updated_at": _now()}
    if idle_vector_link is not None:
        fields["source_assets.idle_vector_key"] = idle_vector_link
    fields["failure_reason"] = failure_reason  # set on failure, cleared (None) otherwise
    try:
        res = _collection().update_one(_doc_filter(avatar_id), {"$set": fields})
        if res.matched_count == 0:
            logger.warning("[mongo] no %s.%s doc matched _id=%s", MONGO_DB, MONGO_COLLECTION, avatar_id)
        else:
            logger.info("[mongo] %s -> %s%s", avatar_id, status,
                        f" (idle_vector_key set)" if idle_vector_link else "")
    except Exception as e:  # noqa: BLE001 - status tracking must be best-effort
        logger.error("[mongo] failed to update _id=%s: %r", avatar_id, e)


def get_source_image_key(avatar_id: str) -> str | None:
    """Return ``source_assets.image_key`` (the R2 key of the subject image) for a template.

    Unlike :func:`set_status`, this is **not** best-effort: the image is required to
    generate, so DB errors propagate and a missing doc/field returns ``None`` for the
    caller to treat as a hard failure.
    """
    doc = _collection().find_one(_doc_filter(avatar_id), {"source_assets.image_key": 1})
    if not doc:
        logger.warning("[mongo] no %s.%s doc matched _id=%s (source lookup)", MONGO_DB, MONGO_COLLECTION, avatar_id)
        return None
    return (doc.get("source_assets") or {}).get("image_key")


def get_template_summary(avatar_id: str) -> dict | None:
    """Return ``{status, image_key, idle_animation_key}`` for the template, or ``None`` if
    no doc matches. Used by the dry-run preview to validate the lookup without generating."""
    doc = _collection().find_one(
        _doc_filter(avatar_id),
        {"status": 1, "source_assets.image_key": 1, "source_assets.idle_animation_key": 1},
    )
    if not doc:
        return None
    sa = doc.get("source_assets") or {}
    return {
        "status": doc.get("status"),
        "image_key": sa.get("image_key"),
        "idle_animation_key": sa.get("idle_animation_key"),
    }


def set_idle_animation_key(avatar_id: str, animation_key: str) -> None:
    """Write only the R2 idle-animation **key** to ``source_assets.idle_animation_key``,
    without touching ``status``.

    Stores a bucket key (e.g. ``templates/<id>/idle``) to mirror how the input
    ``source_assets.image_key`` is stored — not a presigned/public URL. Called before
    flipping status to ``ready`` so any consumer that observes ``status: "ready"`` is
    guaranteed to also see ``idle_animation_key``. Leaves ``idle_vector_key`` untouched.
    Best-effort.
    """
    try:
        res = _collection().update_one(
            _doc_filter(avatar_id),
            {"$set": {"source_assets.idle_animation_key": animation_key, "updated_at": _now()}},
        )
        if res.matched_count == 0:
            logger.warning("[mongo] no %s.%s doc matched _id=%s (animation-key write)",
                           MONGO_DB, MONGO_COLLECTION, avatar_id)
        else:
            logger.info("[mongo] %s idle_animation_key set", avatar_id)
    except Exception as e:  # noqa: BLE001 - key write is best-effort
        logger.error("[mongo] failed to set idle_animation_key _id=%s: %r", avatar_id, e)


def flam_upload(local_path: str | Path, content_type: str = "video/mp4") -> str:
    """Upload a file to GCS via FLAM Resource API and return the permanent public resource_url.

    The FLAM Resource API handles GCS auth server-side.
    """
    import requests

    filename = Path(local_path).name
    api_url = os.environ.get(
        "FLAM_RESOURCE_API_URL",
        "https://fi.production.flamapis.com/resource-svc/api/v1/resources"
    )

    try:
        # Step 1: get signed URL + resource_url
        logger.info("[flam] requesting signed URL for file: %s", filename)
        resp = requests.post(
            api_url,
            json={"file_name": filename, "type": "application/octet-stream"},
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        resp.raise_for_status()
        api_response = resp.json()

        if api_response.get("status") != 200 or api_response.get("error", False):
            error_msg = api_response.get("error", "Unknown error")
            raise ConnectionError(f"FLAM API returned error: {error_msg}")

        data = api_response.get("data", {})
        signed_url = data.get("signed_url")
        resource_url = data.get("resource_url")

        if not signed_url or not resource_url:
            raise ValueError("API response missing signed_url or resource_url")

        logger.info("[flam] received signed URL")

        # Step 2: upload file to signed URL
        logger.info("[flam] uploading file to signed URL: %s", local_path)
        with open(local_path, "rb") as f:
            file_content = f.read()

        upload_resp = requests.put(
            signed_url,
            data=file_content,
            headers={"Content-Type": "application/octet-stream"},
            timeout=300
        )
        upload_resp.raise_for_status()

        logger.info("[flam] uploaded %s -> %s", filename, resource_url)
        return resource_url

    except requests.exceptions.RequestException as e:
        logger.error("[flam] upload failed: %r", e, exc_info=True)
        raise ConnectionError(f"FLAM upload failed: {e}") from e
    except Exception as e:
        logger.error("[flam] unexpected error: %r", e, exc_info=True)
        raise ConnectionError(f"FLAM upload error: {e}") from e


def _jobs_collection():
    """Lazily get the motion_transfer_jobs collection from the same DB."""
    from pymongo import MongoClient
    uri = os.environ["MONGODB_URI"]
    client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
    return client[MONGO_DB]["motion_transfer_jobs"]


def create_job(request_id: str, endpoint: str, **job_data) -> dict:
    """Create a job record in MongoDB. Returns the created document."""
    doc = {
        "request_id": request_id,
        "endpoint": endpoint,  # "generate" or "idle-motion"
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        **job_data
    }
    result = _jobs_collection().insert_one(doc)
    logger.info("[jobs] created request_id=%s (endpoint=%s)", request_id, endpoint)
    return {**doc, "_id": result.inserted_id}


def update_job(request_id: str, **fields) -> None:
    """Update job status and other fields."""
    try:
        fields["updated_at"] = _now()
        _jobs_collection().update_one(
            {"request_id": request_id},
            {"$set": fields}
        )
        logger.info("[jobs] updated request_id=%s: %s", request_id, fields)
    except Exception as e:
        logger.error("[jobs] failed to update request_id=%s: %r", request_id, e)


def get_job(request_id: str) -> dict | None:
    """Retrieve a job record by request_id."""
    try:
        return _jobs_collection().find_one({"request_id": request_id})
    except Exception as e:
        logger.error("[jobs] failed to get request_id=%s: %r", request_id, e)
        return None
