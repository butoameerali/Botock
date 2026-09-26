"""
Image Generation Service powered by Google Flow AI (Nano Banana 2).

This service automates image generation using Google Flow's Nano Banana 2 engine:
- Single unified flagship model: Nano Banana 2
- Multiple aspect ratios: 1:1 (Square), 16:9 (Landscape), 9:16 (Portrait), 4:3, 3:4
- Image ingredient / visual reference support
- High-resolution rendering directly downloaded from Flow Studio
"""

import os
import logging
from app.config import settings
from app.services.flow_service import flow_service

logger = logging.getLogger(__name__)


class NanoBananaImageService:
    def __init__(self):
        self.images_dir = settings.IMAGES_DIR
        os.makedirs(self.images_dir, exist_ok=True)

    async def generate_image(
        self,
        prompt: str,
        generation_id: str,
        status_dict: dict,
        aspect_ratio: str = "1:1",
        model: str = "nano-banana-2",
        image_base64: str = None,
        style: str = None,
        is_pro: bool = False,
    ):
        """
        Delegates image generation to Google Flow AI with Nano Banana 2.
        """
        logger.info(f"[{generation_id}] Delegating to Flow AI Nano Banana 2 (ratio={aspect_ratio})...")
        await flow_service.generate_image(
            prompt=prompt,
            generation_id=generation_id,
            status_dict=status_dict,
            aspect_ratio=aspect_ratio,
            model=model,
            image_base64=image_base64,
            style=style,
            is_pro=is_pro,
        )


image_service = NanoBananaImageService()
