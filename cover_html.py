#!/usr/bin/env python3
"""
Cover generator using HTML/CSS + Playwright.
Black/Yellow high-contrast style inspired by extreme sports thumbnails.
Usage: cover_html.py <video_dir> [title_text] [--bg <bg_image_path>]
Output: cover_new.jpg (1280x720)
"""
import sys
import os
import json
import subprocess
import glob
import re
import random
import base64
from pathlib import Path

# Default background images from cover.md
COVER_MD_PATH = "/Users/dirtclaw/zhaoran/dirtclaw-obsidian/dirtclaw-obsidian/cover.md"
COVER_IMAGE_DIR = "/Users/dirtclaw/zhaoran/dirtclaw-obsidian/dirtclaw-obsidian"


def get_background_images():
    """Get all available background images from the Obsidian directory."""
    images = []

    # Scan directory for all Pasted image files
    if os.path.exists(COVER_IMAGE_DIR):
        for filename in os.listdir(COVER_IMAGE_DIR):
            if filename.startswith("Pasted image") and filename.endswith(".png"):
                img_path = os.path.join(COVER_IMAGE_DIR, filename)
                if os.path.isfile(img_path):
                    images.append(img_path)

    # Also try to parse cover.md for additional context
    if os.path.exists(COVER_MD_PATH) and not images:
        with open(COVER_MD_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        # Match Obsidian image syntax: ![[Pasted image xxx.png]]
        pattern = r'!\[\[(Pasted image [^\]]+\.png)\]\]'
        matches = re.findall(pattern, content)
        for img_name in matches:
            img_path = os.path.join(COVER_IMAGE_DIR, img_name)
            if os.path.exists(img_path) and img_path not in images:
                images.append(img_path)

    return sorted(images)


def select_background_image():
    """Select a random background image from cover.md."""
    images = get_background_images()
    if images:
        return random.choice(images)
    return None


def generate_cover_html(title_text: str, bg_frame_path: str = None, rider_name: str = "") -> str:
    """Generate HTML/CSS cover with black/yellow high-contrast style."""

    # Parse title into main title and subtitle
    def split_title(text):
        # Split by common separators
        if '，' in text or ',' in text:
            parts = [p.strip() for p in text.replace(',', '，').split('，') if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
            elif len(parts) == 1:
                return parts[0], ""
        # Split by space
        words = text.strip().split()
        if len(words) >= 4:
            mid = len(words) // 2
            return ''.join(words[:mid]), ''.join(words[mid:])
        elif len(words) >= 2:
            return words[0], ''.join(words[1:])
        return text, ""

    main_title, sub_title = split_title(title_text)

    # Background image style - use base64 to ensure Playwright can load it
    if bg_frame_path and os.path.exists(bg_frame_path):
        # Read image and convert to base64
        with open(bg_frame_path, 'rb') as img_file:
            img_data = base64.b64encode(img_file.read()).decode('utf-8')
        # Determine mime type based on extension
        ext = os.path.splitext(bg_frame_path)[1].lower()
        mime_type = 'image/png' if ext == '.png' else 'image/jpeg'
        bg_style = f"""
            background-image: url('data:{mime_type};base64,{img_data}');
            background-size: cover;
            background-position: center;
        """
    else:
        bg_style = """
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=1280, height=720">
    <title>Cover</title>
    <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700;900&family=Impact&display=swap" rel="stylesheet">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            width: 1280px;
            height: 720px;
            overflow: hidden;
            position: relative;
            font-family: 'Noto Sans SC', 'Impact', sans-serif;
            {bg_style}
        }}

        /* Bottom darkening gradient for text readability */
        .bottom-overlay {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 450px;
            background: linear-gradient(
                to top,
                rgba(0, 0, 0, 0.92) 0%,
                rgba(0, 0, 0, 0.7) 35%,
                rgba(0, 0, 0, 0.3) 65%,
                transparent 100%
            );
        }}

        /* Series tag - top right */
        .series-tag {{
            position: absolute;
            top: 30px;
            right: 40px;
            background: #FFD700;
            color: #000;
            padding: 10px 24px;
            font-size: 20px;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: 3px;
            box-shadow: 0 4px 20px rgba(255, 215, 0, 0.4);
        }}

        /* Main title container - black background */
        .title-container {{
            position: absolute;
            top: 100px;
            left: 50px;
            right: 50px;
            background: rgba(0, 0, 0, 0.92);
            padding: 25px 35px;
            z-index: 10;
        }}

        /* Quote marks */
        .quote-mark {{
            font-family: Georgia, serif;
            font-size: 56px;
            color: #FFD700;
            line-height: 0.8;
            vertical-align: top;
            margin: 0 5px;
        }}

        /* Main title - golden yellow */
        .main-title {{
            font-size: 68px;
            font-weight: 900;
            color: #FFD700;
            line-height: 1.2;
            letter-spacing: 4px;
            text-shadow: 0 2px 10px rgba(0,0,0,0.5);
        }}

        /* Subtitle - white with left border */
        .sub-title {{
            font-size: 36px;
            font-weight: 700;
            color: #ffffff;
            margin-top: 18px;
            padding-left: 15px;
            border-left: 8px solid #FFD700;
            line-height: 1.3;
        }}

        /* Accent line */
        .accent-line {{
            position: absolute;
            top: 260px;
            left: 50px;
            width: 180px;
            height: 8px;
            background: #FFD700;
            z-index: 10;
        }}

        /* Speed lines decoration */
        .speed-lines {{
            position: absolute;
            bottom: 200px;
            right: 60px;
            z-index: 5;
        }}

        .speed-line {{
            height: 4px;
            background: linear-gradient(90deg, transparent, #FFD700, transparent);
            margin-bottom: 12px;
        }}

        .speed-line:nth-child(1) {{ width: 280px; }}
        .speed-line:nth-child(2) {{ width: 200px; margin-left: 40px; }}
        .speed-line:nth-child(3) {{ width: 140px; margin-left: 80px; }}

        /* Rider name - bottom left */
        .rider-name {{
            position: absolute;
            bottom: 50px;
            left: 50px;
            background: rgba(0, 0, 0, 0.9);
            padding: 12px 30px;
            font-size: 26px;
            font-weight: 900;
            color: #FFD700;
            text-transform: uppercase;
            letter-spacing: 4px;
            border: 2px solid #FFD700;
            z-index: 10;
        }}

        /* Corner accent */
        .corner-accent {{
            position: absolute;
            top: 100px;
            left: 20px;
            width: 30px;
            height: 150px;
            background: #FFD700;
            z-index: 5;
        }}
    </style>
</head>
<body>
    <!-- Bottom darkening overlay -->
    <div class="bottom-overlay"></div>

    <!-- Corner accent -->
    <div class="corner-accent"></div>

    <!-- Series tag -->
    <div class="series-tag">越野摩托</div>

    <!-- Main title container -->
    <div class="title-container">
        <div class="main-title">
            <span class="quote-mark">"</span>{main_title}<span class="quote-mark">"</span>
        </div>
        {f'<div class="sub-title">{sub_title}</div>' if sub_title else ''}
    </div>

    <!-- Accent line -->
    <div class="accent-line"></div>

    <!-- Speed lines -->
    <div class="speed-lines">
        <div class="speed-line"></div>
        <div class="speed-line"></div>
        <div class="speed-line"></div>
    </div>

    <!-- Rider name -->
    {f'<div class="rider-name">{rider_name}</div>' if rider_name else ''}
</body>
</html>"""

    return html


def capture_screenshot(html_content: str, output_path: str):
    """Use Playwright to capture screenshot."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 720})
        page.set_content(html_content)
        # Wait for fonts to load
        page.wait_for_timeout(1200)
        page.screenshot(path=output_path, type='jpeg', quality=95)
        browser.close()


def extract_frame(video_path: str, output_path: str, timestamp: float = 0.6):
    """Extract a frame from video at specified timestamp (default 60%)."""
    ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'

    # Get video duration
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'csv=p=0', video_path],
        capture_output=True, text=True
    )
    duration = float(result.stdout.strip() or '60')
    t = round(duration * timestamp, 1)

    # Extract frame
    subprocess.run([
        ffmpeg, '-y', '-ss', str(t), '-i', video_path,
        '-frames:v', '1', '-q:v', '2', output_path
    ], capture_output=True)

    return os.path.exists(output_path)


def extract_rider_name(video_title: str) -> str:
    """Extract rider name from video title."""
    # Common rider names in dirtbike videos
    import re

    # Look for capitalized names (e.g., "Luke Fauser", "Jalek Swoll")
    name_pattern = r'\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b'
    matches = re.findall(name_pattern, video_title)

    if matches:
        # Return the first match, convert to uppercase
        return matches[0].upper()

    return ""


def main():
    if len(sys.argv) < 2:
        print('Usage: cover_html.py <video_dir> [title_text] [--bg <image_path|random|video>]')
        print('  --bg random    : Use random image from cover.md')
        print('  --bg video     : Extract frame from video (default)')
        print('  --bg <path>    : Use specific image path')
        sys.exit(1)

    video_dir = sys.argv[1]
    bg_source = "video"  # default: extract from video
    title = None

    # Parse arguments
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == '--bg':
            if i + 1 < len(sys.argv):
                bg_source = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        elif title is None:
            title = sys.argv[i]
            i += 1
        else:
            title += " " + sys.argv[i]
            i += 1

    # Get title from meta.json if not provided
    if not title:
        meta_path = os.path.join(video_dir, 'meta.json')
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
                title = meta.get('title', '越野摩托车技术解析')
        else:
            # Try to extract from directory name
            dir_name = os.path.basename(video_dir)
            # Remove youtube ID prefix
            title = re.sub(r'^[A-Za-z0-9_-]{11}-', '', dir_name).strip()
            title = title.replace('_', ' ').replace('-', ' ')
            if not title:
                title = '越野摩托车技术解析'

    # Extract rider name from video directory name
    rider_name = extract_rider_name(os.path.basename(video_dir))

    # Determine background image source
    bg_frame = None

    if bg_source == "random":
        # Use random image from cover.md
        bg_frame = select_background_image()
        if bg_frame:
            print(f'Using random background from cover.md: {os.path.basename(bg_frame)}')
        else:
            print('WARNING: No background images found in cover.md, falling back to video frame')
            bg_source = "video"

    elif bg_source and bg_source not in ["video", "random"]:
        # Use specific image path
        if os.path.exists(bg_source):
            bg_frame = bg_source
            print(f'Using specified background: {bg_frame}')
        else:
            print(f'WARNING: Background image not found: {bg_source}, falling back to video frame')
            bg_source = "video"

    if bg_source == "video" or not bg_frame:
        # Find video file and extract frame
        videos = [f for f in glob.glob(os.path.join(video_dir, '*.mp4')) if '_subbed' not in f]
        if not videos:
            videos = glob.glob(os.path.join(video_dir, '*.mp4'))

        if videos:
            # Extract frame for background
            frame_path = os.path.join(video_dir, '_bg_frame.jpg')
            if extract_frame(videos[0], frame_path):
                bg_frame = frame_path
                print(f'Extracted background frame from video: {frame_path}')

    # Generate HTML
    print(f'Generating cover for: {title}')
    html = generate_cover_html(title, bg_frame, rider_name)

    # Save HTML for debugging (optional)
    html_path = os.path.join(video_dir, '_cover.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML saved: {html_path}')

    # Capture screenshot
    output_path = os.path.join(video_dir, 'cover_new.jpg')
    print('Capturing screenshot with Playwright...')
    capture_screenshot(html, output_path)

    print(f'Cover saved: {output_path}')

    # Clean up temp frame
    if bg_frame and os.path.exists(bg_frame):
        os.remove(bg_frame)


if __name__ == '__main__':
    import re
    main()
