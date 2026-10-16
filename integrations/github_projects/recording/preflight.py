"""Maintained DocumentAnalysis browser/encoder preflight, without hosted auth dependencies."""
import os
import json
import subprocess
import sys
from pathlib import Path

class CaptureError(ValueError):
    pass

def preflight_browser(playwright, args):
    installed = Path(getattr(playwright, args.engine).executable_path)
    browser = Path(args.browser) if args.browser else installed
    if not browser.is_file() or not os.access(browser, os.X_OK):
        raise CaptureError('Browser executable is missing or not executable. Set PLAYWRIGHT_BROWSERS_PATH '
                           'to the installed cache, or run python -m playwright install ' + args.engine + '.')
    # executable_path is resolved by the running Playwright driver, including
    # PLAYWRIGHT_BROWSERS_PATH=0 and its normal platform-specific cache location.
    build = next((parent for parent in installed.parents
                  if parent.name.startswith(args.engine + '-')), None)
    if build is None:
        raise CaptureError('Cannot resolve the selected Playwright browser cache.')
    import playwright as package
    try:
        metadata = json.loads((Path(package.__file__).parent / 'driver' / 'package' / 'browsers.json').read_text())
        revision = next(item['revision'] for item in metadata['browsers'] if item['name'] == 'ffmpeg')
    except (OSError, KeyError, StopIteration, json.JSONDecodeError):
        raise CaptureError('Reinstall the configured Playwright runtime; encoder metadata is unavailable.') from None
    name = {'linux': 'ffmpeg-linux', 'darwin': 'ffmpeg-mac', 'win32': 'ffmpeg-win64.exe'}.get(sys.platform)
    if not name:
        raise CaptureError('This operating system is unsupported by the recording preflight.')
    encoder = build.parent / ('ffmpeg-' + revision) / name
    if not encoder.is_file() or not os.access(encoder, os.X_OK):
        raise CaptureError('Playwright video encoder is missing from its selected cache. '
                           'Set PLAYWRIGHT_BROWSERS_PATH to the cache containing both browser and ffmpeg, '
                           'then run python -m playwright install ' + args.engine + '. '
                           '--browser alone does not select the video encoder cache.')
    try:
        subprocess.run([str(encoder), '-version'], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise CaptureError('The selected Playwright encoder cannot run; reinstall the selected browser runtime.') from None
    args.browser = str(browser.resolve())
