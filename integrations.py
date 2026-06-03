"""MongoDB status tracking + Cloudflare R2 upload for the /idle-motion endpoint.

Credentials come from environment variables (Modal secrets):
  MONGODB_URI                          mongodb+srv://<user>:<pass>@dev0.4xtrj.mongodb.net/...
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL

Mongo target: DB ``Faceshot`` (override MONGODB_DB), collection ``idle_motion``
(override MONGODB_COLLECTION). Document shape (keyed by avatarid):
  {avatarid: str, status: "processing" | "done processing" | "failed", link: str, updated_at: str}

pymongo/boto3 are imported lazily so importing this module stays cheap and CUDA-free
(safe for the Modal memory-snapshot path).
"""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MONGO_DB = os.environ.get("MONGODB_DB", "Faceshot")
MONGO_COLLECTION = os.environ.get("MONGODB_COLLECTION", "idle_motion")

_mongo_lock = threading.Lock()
_mongo_coll = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def set_status(avatar_id: str, status: str, link: str | None = None) -> None:
    """Upsert the avatar's idle-motion doc with the given status (and link when done).

    Never raises — a DB hiccup must not crash the generation job; it's logged instead.
    """
    update = {"avatarid": avatar_id, "status": status, "updated_at": _now()}
    if link is not None:
        update["link"] = link
    try:
        _collection().update_one({"avatarid": avatar_id}, {"$set": update}, upsert=True)
        logger.info("[mongo] %s -> %s%s", avatar_id, status, f" ({link})" if link else "")
    except Exception as e:  # noqa: BLE001 - status tracking must be best-effort
        logger.error("[mongo] failed to set status for avatarid=%s: %r", avatar_id, e)


def r2_upload(local_path: str | Path, key: str, content_type: str = "video/mp4") -> str:
    """Upload a local file to the R2 bucket under ``key`` and return its public URL."""
    import boto3

    account = os.environ["R2_ACCOUNT_ID"]
    bucket = os.environ["R2_BUCKET"]
    public_base = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    client.upload_file(str(local_path), bucket, key, ExtraArgs={"ContentType": content_type})
    url = f"{public_base}/{key}"
    logger.info("[r2] uploaded %s -> %s", key, url)
    return url
