"""
Google Flow Creative Suite Service (Flow AI Studio Automation).

This service automates Google Flow (flow.google.com) using Playwright.
It handles:
1. Validates saved session cookies
2. Navigates cleanly into Flow Studio (handles home page, existing projects, onboarding overlays)
3. Configures settings for Video (Omni 1.1 Flash, 360p, 10s, x1, 16:9 / 9:16)
4. Configures settings for Image (Nano Banana 2, x1, 1:1 / 16:9 / 9:16 / 4:3 / 3:4)
5. Attaches ingredient / reference images via the Flow file upload chooser
6. Types prompt into the ProseMirror editor and submits
7. Auto-approves credit deductions if prompted
8. Polls for video rendering (downloads MP4) or image generation (downloads high-res PNG)
"""

import os
import re
import time
import base64
import shutil
import asyncio
import tempfile
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
from app.config import settings

logger = logging.getLogger(__name__)


def get_chrome_path():
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/bin/google-chrome",
        "/usr/bin/google-chrome",
        "/opt/google/chrome/chrome",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


class FlowService:
    def __init__(self):
        self.session_path = settings.SESSION_PATH
        self.videos_dir = settings.VIDEOS_DIR
        self.images_dir = settings.IMAGES_DIR
        self.debug_dir = settings.DEBUG_DIR
        self.current_project_url = None
        self.project_scene_count = 0
        os.makedirs(self.videos_dir, exist_ok=True)
        os.makedirs(self.images_dir, exist_ok=True)
        os.makedirs(self.debug_dir, exist_ok=True)

    def check_session_valid(self) -> bool:
        return os.path.exists(self.session_path)

    async def _launch_browser(self):
        chrome_bin = get_chrome_path()
        launch_args = {
            "headless": True,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        }
        if chrome_bin:
            launch_args["executable_path"] = chrome_bin

        if settings.PROXY_SERVER:
            proxy_cfg = {"server": settings.PROXY_SERVER}
            if settings.PROXY_USER:
                proxy_cfg["username"] = settings.PROXY_USER
                proxy_cfg["password"] = settings.PROXY_PASS
            launch_args["proxy"] = proxy_cfg

        p = await async_playwright().start()
        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context(
            storage_state=self.session_path,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await Stealth().apply_stealth_async(page)
        except Exception as e:
            logger.debug(f"Stealth note: {e}")

        return p, browser, context, page

    async def _ensure_studio_page(self, page, generation_id: str, status_dict: dict):
        """
        Guarantees that the browser is inside an active Flow Studio project,
        dismisses any explore/onboarding modals, and returns the ProseMirror prompt editor.
        """
        if self.current_project_url and self.project_scene_count < 10:
            logger.info(f"[{generation_id}] Resuming active studio project ({self.project_scene_count + 1}/10): {self.current_project_url}")
            status_dict[generation_id]["message"] = "Opening studio project..."
            await page.goto(self.current_project_url, timeout=settings.PAGE_LOAD_TIMEOUT * 1000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
        else:
            logger.info(f"[{generation_id}] Navigating to Flow...")
            status_dict[generation_id]["message"] = "Connecting to Flow Studio..."
            await page.goto("https://flow.google.com", timeout=settings.PAGE_LOAD_TIMEOUT * 1000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            if "accounts.google.com" in page.url:
                await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_session_expired.png"))
                raise Exception("Google Flow session expired. Please re-run authentication.")

            if "/project/" not in page.url:
                logger.info(f"[{generation_id}] Opening studio project from home...")
                proj_link = page.locator('a[aria-label="Open project"], a[href*="/project/"]').first
                new_btn = page.locator('button:has-text("New project"), button:has-text("Start Creating"), [aria-label*="New project" i]').first

                if await proj_link.is_visible(timeout=3000):
                    await proj_link.click()
                elif await new_btn.is_visible(timeout=3000):
                    await new_btn.click()

                try:
                    await page.wait_for_url("**/project/**", timeout=20000)
                except Exception:
                    if await proj_link.is_visible(timeout=2000):
                        await proj_link.click()
                        await page.wait_for_url("**/project/**", timeout=15000)

            self.current_project_url = page.url
            self.project_scene_count = 0

        # Dismiss explore tools / onboarding overlay if open
        back_btn = page.locator('button[aria-label*="Back button" i], button:has-text("arrow_back")').first
        if await back_btn.is_visible(timeout=2000):
            await back_btn.click()
            await asyncio.sleep(1)

        all_media_btn = page.locator('button:has-text("All media"), div:has-text("All media")').first
        if await all_media_btn.is_visible(timeout=2000):
            await all_media_btn.click()
            await asyncio.sleep(1)

        prompt_input = page.locator('.ProseMirror, [contenteditable="true"]').first
        await prompt_input.wait_for(state="visible", timeout=25000)
        return prompt_input

    async def _upload_ingredient(self, page, image_base64: str, generation_id: str):
        """
        Uploads ingredient / reference image into Flow Studio via the '+' button and file chooser.
        """
        if not image_base64:
            return

        if "," in image_base64:
            image_base64 = image_base64.split(",", 1)[1]

        try:
            img_bytes = base64.b64decode(image_base64)
        except Exception as e:
            logger.warning(f"[{generation_id}] Invalid base64 image ingredient: {e}")
            return

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name

        try:
            logger.info(f"[{generation_id}] Attaching ingredient image...")
            add_btn = page.locator('button:has-text("+"), button[aria-label*="Add" i]').first
            if await add_btn.is_visible(timeout=3000):
                await add_btn.click()
                await asyncio.sleep(1)
                upload_opt = page.locator('button:has-text("Upload"), [role="menuitem"]:has-text("Upload"), div:has-text("Upload")').last
                if await upload_opt.is_visible(timeout=3000):
                    async with page.expect_file_chooser(timeout=8000) as fc_info:
                        await upload_opt.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(tmp_path)
                    logger.info(f"[{generation_id}] Ingredient uploaded to Flow successfully.")
                    await asyncio.sleep(2.5)
        except Exception as e:
            logger.warning(f"[{generation_id}] Ingredient upload note: {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def _handle_credit_approval(self, page, generation_id: str):
        """Clicks 'Always approve' or 'Approve' if credit confirmation dialog appears."""
        for _ in range(5):
            approve_btn = page.get_by_text("Always approve").first
            if not await approve_btn.is_visible(timeout=1000):
                approve_btn = page.get_by_text("Approve").first

            if await approve_btn.is_visible(timeout=1000):
                logger.info(f"[{generation_id}] Credit approval dialog found! Clicking approve...")
                await approve_btn.click()
                await asyncio.sleep(1.5)
                break
            await asyncio.sleep(1.5)

    # =========================================================================
    # VIDEO GENERATION (Omni 1.1 Flash, 360p, 10s, x1, 16:9 / 9:16)
    # =========================================================================
    async def generate_video(
        self,
        prompt: str,
        generation_id: str,
        status_dict: dict,
        is_pro: bool = False,
        model: str = "omni-1.1-flash-360p",
        aspect_ratio: str = "16:9",
        motion_hint: str = None,
        image_base64: str = None,
        reference_image_path: str = None,
    ):
        if not self.check_session_valid():
            status_dict[generation_id] = {
                "status": "failed",
                "message": "Session not found. Please log in to Google Flow first."
            }
            return

        status_dict[generation_id] = {
            "status": "processing",
            "message": "Connecting to Google Flow AI..."
        }

        p, browser, context, page = await self._launch_browser()

        try:
            prompt_input = await self._ensure_studio_page(page, generation_id, status_dict)

            # Upload ingredient image if provided
            if image_base64:
                status_dict[generation_id]["message"] = "Uploading image ingredient..."
                await self._upload_ingredient(page, image_base64, generation_id)

            # Configure Settings Popup for Video
            status_dict[generation_id]["message"] = "Configuring Flow AI video engine..."
            try:
                settings_btn = page.locator('button[aria-label*="Settings trigger" i], [aria-label="Settings trigger"]')
                if not await settings_btn.is_visible(timeout=3000):
                    settings_btn = page.locator('button:has-text("Banana"), button:has-text("Video"), button:has-text("Omni")').first

                if await settings_btn.is_visible(timeout=3000):
                    await settings_btn.click()
                    await asyncio.sleep(1)

                    # Video Tab
                    video_tab = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("Video")').first
                    if await video_tab.is_visible(timeout=2000):
                        await video_tab.click()
                        await asyncio.sleep(0.5)

                    # Aspect Ratio
                    ratio_to_click = "9:16" if "9:16" in str(aspect_ratio) else "16:9"
                    ratio_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator(f'button:has-text("{ratio_to_click}")').first
                    if await ratio_btn.is_visible(timeout=2000):
                        await ratio_btn.click()
                        await asyncio.sleep(0.3)

                    # Model selection: Omni 1.1 Flash
                    if not is_pro:
                        model_dropdown = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("Omni"), button:has-text("Veo"), [aria-haspopup="listbox"]').first
                        if await model_dropdown.is_visible(timeout=2000):
                            await model_dropdown.click()
                            await asyncio.sleep(0.5)
                            omni_opt = page.locator('text="Omni 1.1 Flash"').first
                            if await omni_opt.is_visible(timeout=2000):
                                await omni_opt.click()
                                await asyncio.sleep(0.3)

                    # Resolution: 360p
                    res_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("360p")').first
                    if await res_btn.is_visible(timeout=2000):
                        await res_btn.click()
                        await asyncio.sleep(0.3)

                    # Duration: 10s (fallback 8s)
                    dur_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("10s")').first
                    if not await dur_btn.is_visible(timeout=1500):
                        dur_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("8s")').first
                    if await dur_btn.is_visible(timeout=2000):
                        await dur_btn.click()
                        await asyncio.sleep(0.3)

                    # Count: x1
                    count_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("x1")').first
                    if await count_btn.is_visible(timeout=2000):
                        await count_btn.click()
                        await asyncio.sleep(0.3)

                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
            except Exception as ex:
                logger.warning(f"[{generation_id}] Video settings note: {ex}")

            # Prompt Construction
            formatted_prompt = prompt.strip()
            if not is_pro:
                formatted_prompt = re.sub(r'(?i)(use\s+)?(veo|quality|1080p|4k)', '', formatted_prompt).strip()

            if not formatted_prompt.lower().startswith("generate a video") and not formatted_prompt.lower().startswith("create a video"):
                formatted_prompt = f"Generate a video: {formatted_prompt}"

            if motion_hint:
                formatted_prompt += f". Camera motion: {motion_hint.strip()}"

            status_dict[generation_id]["message"] = "Entering prompt into Flow AI..."
            await prompt_input.click()
            await asyncio.sleep(0.3)
            await page.keyboard.type(formatted_prompt, delay=15)
            await asyncio.sleep(0.8)

            # Submit
            status_dict[generation_id]["message"] = "Submitting prompt to Flow AI..."
            generate_btn = page.locator('button[aria-label*="Start generation" i], .generate-icon-button, button[type="submit"]').first
            if await generate_btn.is_visible(timeout=3000):
                await generate_btn.click()
            else:
                await page.keyboard.press("Enter")

            await asyncio.sleep(3)
            await self._handle_credit_approval(page, generation_id)

            # Polling for video completion
            status_dict[generation_id]["message"] = "AI is generating your video... Please wait."
            start_time = asyncio.get_event_loop().time()
            video_ready = False
            already_opened = False

            while not video_ready:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > settings.VIDEO_GEN_TIMEOUT:
                    await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_timeout.png"))
                    raise Exception(f"Video generation timed out after {settings.VIDEO_GEN_TIMEOUT}s.")

                mins = int(elapsed // 60)
                secs = int(elapsed % 60)

                try:
                    body_text = await page.evaluate("() => document.body.innerText || ''")
                except Exception:
                    body_text = ""

                pct_matches = re.findall(r'(\d{1,3})%', body_text)
                if pct_matches:
                    latest_pct = pct_matches[-1]
                    status_dict[generation_id]["message"] = (
                        f"Rendering video on Flow AI... ({latest_pct}% completed, {mins}m {secs:02d}s elapsed)"
                    )
                else:
                    status_dict[generation_id]["message"] = (
                        f"Rendering video on Flow AI... ({mins}m {secs:02d}s elapsed)"
                    )

                if elapsed >= 35 and not pct_matches:
                    dl_btn = page.locator('button[aria-label*="Download media" i], button[aria-label*="Download" i]').first
                    if await dl_btn.is_visible():
                        video_ready = True
                        already_opened = True
                        break

                    logger.info(f"[{generation_id}] Checking if video tile finished rendering...")
                    await page.mouse.click(350, 250)
                    await asyncio.sleep(2.5)

                    if await dl_btn.is_visible():
                        video_ready = True
                        already_opened = True
                        break

                await asyncio.sleep(6)

            # Download MP4
            status_dict[generation_id]["message"] = "Downloading your video..."
            file_path = await self._download_video(page, generation_id, already_opened=already_opened)

            status_dict[generation_id].update({
                "status": "completed",
                "download_url": f"/api/video/download/{generation_id}",
                "message": "Video generated successfully!"
            })
            self.project_scene_count += 1
            await context.storage_state(path=self.session_path)

        except Exception as e:
            logger.error(f"[{generation_id}] Video generation failed: {e}")
            await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_video_error.png"))
            status_dict[generation_id].update({
                "status": "failed",
                "message": str(e)
            })
        finally:
            await browser.close()
            await p.stop()

    async def _download_video(self, page, generation_id: str, already_opened: bool = False) -> str:
        file_path = os.path.join(self.videos_dir, f"{generation_id}.mp4")
        dl_btn = page.locator('button[aria-label*="Download media" i], button[aria-label*="Download" i]').first

        if not already_opened or not await dl_btn.is_visible():
            await page.mouse.click(350, 250)
            await asyncio.sleep(2.5)

        if not await dl_btn.is_visible(timeout=8000):
            await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_no_dl_btn.png"))
            raise Exception("Download media button not visible after opening video tile.")

        await dl_btn.click()
        await asyncio.sleep(1.5)

        opt = page.get_by_text("720p").first
        if not await opt.is_visible(timeout=2000):
            opt = page.get_by_text("Original size").first
        if not await opt.is_visible(timeout=2000):
            opt = page.locator('[role="menuitem"], .mat-mdc-menu-item').first

        async with page.expect_download(timeout=90000) as dl_info:
            await opt.click()

        download = await dl_info.value
        await download.save_as(file_path)
        return file_path

    # =========================================================================
    # IMAGE GENERATION (Nano Banana 2, x1, Aspect Ratios)
    # =========================================================================
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
        if not self.check_session_valid():
            status_dict[generation_id] = {
                "status": "failed",
                "message": "Session not found. Please log in to Google Flow first."
            }
            return

        status_dict[generation_id] = {
            "status": "processing",
            "message": "Connecting to Google Flow AI..."
        }

        p, browser, context, page = await self._launch_browser()

        try:
            prompt_input = await self._ensure_studio_page(page, generation_id, status_dict)

            # Upload ingredient image if provided
            if image_base64:
                status_dict[generation_id]["message"] = "Uploading image ingredient..."
                await self._upload_ingredient(page, image_base64, generation_id)

            # Configure Settings Popup for Image
            status_dict[generation_id]["message"] = "Configuring Flow AI Nano Banana 2 engine..."
            try:
                settings_btn = page.locator('button[aria-label*="Settings trigger" i], [aria-label="Settings trigger"]')
                if not await settings_btn.is_visible(timeout=3000):
                    settings_btn = page.locator('button:has-text("Banana"), button:has-text("Video"), button:has-text("Omni")').first

                if await settings_btn.is_visible(timeout=3000):
                    await settings_btn.click()
                    await asyncio.sleep(1)

                    # Image Tab
                    img_tab = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("Image"), [role="tab"]:has-text("Image")').first
                    if await img_tab.is_visible(timeout=2000):
                        await img_tab.click()
                        await asyncio.sleep(0.5)

                    # Aspect Ratio
                    allowed_ratios = ["1:1", "16:9", "9:16", "4:3", "3:4"]
                    ratio_str = aspect_ratio if aspect_ratio in allowed_ratios else "1:1"
                    ratio_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator(f'button:has-text("{ratio_str}")').first
                    if await ratio_btn.is_visible(timeout=2000):
                        await ratio_btn.click()
                        await asyncio.sleep(0.3)

                    # Count: x1
                    count_btn = page.locator('[role="dialog"], [data-floating-ui-portal]').locator('button:has-text("x1")').first
                    if await count_btn.is_visible(timeout=2000):
                        await count_btn.click()
                        await asyncio.sleep(0.3)

                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
            except Exception as ex:
                logger.warning(f"[{generation_id}] Image settings note: {ex}")

            # Capture existing images on canvas
            initial_count = await page.evaluate("() => document.querySelectorAll('img[alt*=\"image\"], img.image').length")
            initial_first_src = await page.evaluate("""() => {
                const tile = document.querySelector('img[alt*="image"], img.image');
                return tile ? tile.src : "";
            }""")
            existing_imgs = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('img')).map(i => i.src).filter(s => s.includes('asb/') || s.includes('googleusercontent.com'));
            }""")

            # Enter Prompt
            full_prompt = prompt.strip()
            if style:
                full_prompt += f", {style} style"

            status_dict[generation_id]["message"] = "Entering prompt into Flow AI..."
            await prompt_input.click()
            await asyncio.sleep(0.3)
            await page.keyboard.type(full_prompt, delay=12)
            await asyncio.sleep(0.8)

            # Submit
            status_dict[generation_id]["message"] = "Generating image with Nano Banana 2..."
            generate_btn = page.locator('button[aria-label*="Start generation" i], .generate-icon-button, button[type="submit"]').first
            if await generate_btn.is_visible(timeout=3000):
                await generate_btn.click()
            else:
                await page.keyboard.press("Enter")

            await asyncio.sleep(3)
            await self._handle_credit_approval(page, generation_id)

            # Polling for Image completion
            status_dict[generation_id]["message"] = "Synthesizing Nano Banana 2 image... Please wait."
            found_img_url = None
            save_path = os.path.join(self.images_dir, f"{generation_id}.png")

            start_time = asyncio.get_event_loop().time()
            for attempt in range(45):  # 45 * 2s = 90 seconds max
                await asyncio.sleep(2)
                elapsed = asyncio.get_event_loop().time() - start_time

                try:
                    body_text = await page.evaluate("() => document.body.innerText || ''")
                except Exception:
                    body_text = ""

                pct_matches = re.findall(r'(\d{1,3})%', body_text)
                if pct_matches:
                    latest_pct = pct_matches[-1]
                    status_dict[generation_id]["message"] = f"Rendering with Nano Banana 2... ({latest_pct}%)"
                    logger.info(f"[{generation_id}] Image render in progress: {latest_pct}%")
                    continue
                else:
                    status_dict[generation_id]["message"] = f"Rendering with Nano Banana 2... ({int(elapsed)}s elapsed)"

                # Flow images finish in ~15-35s. After 15s and no percentage, download image
                if elapsed >= 15:
                    logger.info(f"[{generation_id}] Checking if image finished...")
                    dl_btn = page.locator('button[aria-label*="Download" i]').first
                    if not await dl_btn.is_visible():
                        tiles = await page.locator('img[alt*="user\'s image"], img[alt*="image"]').all()
                        if tiles:
                            await tiles[0].click()
                        else:
                            await page.mouse.click(250, 200)
                        await asyncio.sleep(2)

                    if await dl_btn.is_visible():
                        logger.info(f"[{generation_id}] Image viewer ready, downloading original resolution...")
                        await dl_btn.click()
                        await asyncio.sleep(1)

                        opt = page.locator('button:has-text("Original size"), [role="menuitem"]:has-text("Original size"), button:has-text("1K")').first
                        if not await opt.is_visible(timeout=3000):
                            opt = page.locator('[role="menuitem"]').first

                        async with page.expect_download(timeout=30000) as dl_info:
                            await opt.click()

                        download = await dl_info.value
                        await download.save_as(save_path)
                        logger.info(f"[{generation_id}] Image saved: {save_path} ({os.path.getsize(save_path)} bytes)")
                        found_img_url = save_path
                        break

            if not found_img_url or not os.path.exists(save_path):
                await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_img_timeout.png"))
                raise Exception("Image generation timed out waiting for image tile.")

            download_url = f"/api/image/download/{generation_id}"
            status_dict[generation_id].update({
                "status": "completed",
                "message": "Image generated successfully!",
                "download_url": download_url,
                "image_url": download_url,
            })
            self.project_scene_count += 1
            await context.storage_state(path=self.session_path)

        except Exception as e:
            logger.error(f"[{generation_id}] Image generation failed: {e}")
            await page.screenshot(path=os.path.join(self.debug_dir, f"{generation_id}_image_error.png"))
            status_dict[generation_id].update({
                "status": "failed",
                "message": str(e)
            })
        finally:
            await browser.close()
            await p.stop()

    async def reset_session(self):
        self.current_project_url = None
        self.project_scene_count = 0
        return {"status": "success", "message": "Session reset successfully."}


flow_service = FlowService()
FlowVideoService = FlowService
