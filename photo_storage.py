"""
Handles uploading shelf photos to Cloudinary so we have a permanent, clickable
image_url to store in master_table (Flask's in-memory image would otherwise
disappear right after the request finishes).
"""

import os
import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
)


def upload_image(image_bytes, folder="perfume_extractor"):
    """
    Uploads image bytes to Cloudinary and returns a public HTTPS URL.
    Returns None (instead of raising) if Cloudinary isn't configured or the upload fails —
    so a missing/broken photo upload never blocks saving the actual item data.
    """
    if not os.environ.get("CLOUDINARY_CLOUD_NAME"):
        print("[photo_storage] Cloudinary not configured — skipping photo upload, image_url will be blank.")
        return None

    print("[photo_storage] Uploading photo to Cloudinary...")
    try:
        result = cloudinary.uploader.upload(
            image_bytes,
            folder=folder,
            resource_type="image",
        )
        url = result.get("secure_url")
        print(f"[photo_storage] Upload successful: {url}")
        return url
    except Exception as e:
        print(f"[photo_storage] Upload failed: {e}")
        return None