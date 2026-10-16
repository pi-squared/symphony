"""Decode the video player from the persisted GitHub issue comment HTML."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from playwright.sync_api import sync_playwright
from recording.preflight import preflight_browser

request = json.loads(Path(sys.argv[1]).read_text())
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = request["browser_cache"]
with sync_playwright() as playwright:
    args = SimpleNamespace(engine="chromium", browser=None)
    preflight_browser(playwright, args)
    browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.set_content(request["comment_html"])
    video = page.locator("video")
    assert video.count() == 1
    video.evaluate("async v => { v.muted = true; await v.play(); }")
    page.wait_for_function("() => { const v=document.querySelector('video'); return v.currentTime>=1 && v.videoWidth===1440 && v.videoHeight===900 && !v.error; }", timeout=30000)
    result = video.evaluate("v => ({width:v.videoWidth,height:v.videoHeight,currentTime:v.currentTime,duration:v.duration})")
    assert abs(result["duration"] - request["duration"]) < .2
    browser.close()
print(json.dumps({"persisted_comment_playback_verified": True, **result}))
