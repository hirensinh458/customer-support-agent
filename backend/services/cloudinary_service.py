# backend/services/cloudinary_service.py

import logging
import cloudinary
import cloudinary.uploader
from backend.core.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()


def init_cloudinary() -> None:
    """
    Configure the Cloudinary SDK with credentials from settings.
    Called once at app startup (lifespan) — same pattern as connect_db().
    """
    cloudinary.config(
        cloud_name = settings.cloudinary_cloud_name,
        api_key    = settings.cloudinary_api_key,
        api_secret = settings.cloudinary_api_secret,
        secure     = True,
    )
    logger.info(f"Cloudinary configured — cloud: {settings.cloudinary_cloud_name}")


async def upload_image(
    file_bytes:  bytes,
    public_id:   str,
    folder:      str = "leafy/defect_photos",
) -> dict:
    """
    Upload image bytes to Cloudinary.

    Args:
        file_bytes : raw bytes of the image file
        public_id  : unique identifier within the folder
                     (use pending_request_id so we can find & delete it later)
        folder     : Cloudinary folder path

    Returns:
        {"secure_url": str, "public_id": str}  on success
        raises Exception on failure — caller wraps in try/except
    """
    result = cloudinary.uploader.upload(
        file_bytes,
        public_id       = public_id,
        folder          = folder,
        resource_type   = "image",
        overwrite       = True,
        invalidate      = True,
    )
    logger.info(f"Cloudinary upload — public_id={result['public_id']}")
    return {
        "secure_url": result["secure_url"],
        "public_id":  result["public_id"],
    }


async def delete_image(public_id: str) -> bool:
    """
    Delete an image from Cloudinary by its full public_id.

    Returns True if deleted, False if not found / already gone.
    Does NOT raise — a failed delete should never crash user-facing code.
    """
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="image")
        deleted = result.get("result") == "ok"
        if deleted:
            logger.info(f"Cloudinary delete — public_id={public_id}")
        else:
            logger.warning(f"Cloudinary delete: not found — public_id={public_id}")
        return deleted
    except Exception as e:
        logger.warning(f"Cloudinary delete failed — public_id={public_id}: {e}")
        return False