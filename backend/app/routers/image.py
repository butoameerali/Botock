import os
import re
import uuid
import asyncio
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from app.models.schemas import ImageGenerateRequest, ImageGenerateResponse, ImageStatusResponse, ImageListResponse
from app.services.image_service import NanoBananaImageService
from app.middleware.auth import check_daily_image_quota, get_current_user
from app.config import settings

router = APIRouter(prefix="/api/image", tags=["Image"])

image_statuses = {}
image_service = NanoBananaImageService()

UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

def validate_uuid(val: str):
    if not UUID_REGEX.match(val):
        raise HTTPException(status_code=400, detail="Invalid generation ID format.")


async def safe_image_generate_task(
    prompt: str,
    generation_id: str,
    aspect_ratio: str,
    model: str,
    style: str = None,
    image_base64: str = None,
    is_pro: bool = False,
):
    await image_service.generate_image(
        prompt=prompt,
        generation_id=generation_id,
        status_dict=image_statuses,
        aspect_ratio=aspect_ratio,
        model=model,
        image_base64=image_base64,
        style=style,
        is_pro=is_pro,
    )


@router.post("/generate", response_model=ImageGenerateResponse)
async def generate_image(
    request: ImageGenerateRequest,
    req: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(check_daily_image_quota),
):
    clean_prompt = request.prompt.strip()
    is_pro = user.get("is_pro", False)

    selected_model = request.model or "nano-banana-2"
    # Guard: Free users can only use nano-banana-lite or nano-banana-2
    if not is_pro and selected_model not in ["nano-banana-lite", "nano-banana-2"]:
        selected_model = "nano-banana-2"

    allowed_aspect_ratios = ["16:9", "4:3", "1:1", "3:4", "9:16"]
    selected_ratio = request.aspect_ratio if request.aspect_ratio in allowed_aspect_ratios else "1:1"

    generation_id = str(uuid.uuid4())
    image_statuses[generation_id] = {
        "user_id": user["user_id"],
        "status": "queued",
        "message": f"Queued {selected_model.replace('-', ' ').title()} generation ({selected_ratio})...",
    }

    background_tasks.add_task(
        safe_image_generate_task,
        clean_prompt,
        generation_id,
        selected_ratio,
        selected_model,
        request.style,
        request.image_base64,
        is_pro,
    )

    return ImageGenerateResponse(
        generation_id=generation_id,
        status="queued",
        message="Image generation queued successfully.",
    )


@router.get("/status/{generation_id}", response_model=ImageStatusResponse)
async def get_image_status(generation_id: str, user: dict = Depends(get_current_user)):
    validate_uuid(generation_id)

    if generation_id not in image_statuses:
        raise HTTPException(status_code=404, detail="Image generation ID not found.")

    data = image_statuses[generation_id]
    if data.get("user_id") and data.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied: You do not own this image generation.")

    return ImageStatusResponse(
        generation_id=generation_id,
        status=data.get("status", "unknown"),
        message=data.get("message"),
        download_url=data.get("download_url"),
        image_url=data.get("image_url"),
    )


@router.get("/download/{generation_id}")
async def download_image(generation_id: str, user: dict = Depends(get_current_user)):
    validate_uuid(generation_id)

    # Ownership check
    if generation_id in image_statuses:
        data = image_statuses[generation_id]
        if data.get("user_id") and data.get("user_id") != user["user_id"]:
            raise HTTPException(status_code=403, detail="Access denied: You do not own this image generation.")

    safe_filename = f"{generation_id}.png"
    file_path = os.path.realpath(os.path.join(settings.IMAGES_DIR, safe_filename))
    expected_dir = os.path.realpath(settings.IMAGES_DIR)

    if not file_path.startswith(expected_dir):
        raise HTTPException(status_code=403, detail="Access denied.")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image file not found or not yet generated.")

    return FileResponse(path=file_path, media_type="image/png", filename=f"botock-image-{safe_filename}")
