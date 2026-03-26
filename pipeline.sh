#!/usr/bin/env bash
# Dirtbike pipeline: download → transcribe → ASR correct → translate → validate → cover → burn → upload
# Usage: pipeline.sh <YouTube_URL>
set -e

URL="$1"
[ -z "$URL" ] && { echo "Usage: $0 <YouTube_URL>"; exit 1; }

YTDLP=$(command -v yt-dlp || echo ~/Library/Python/3.9/bin/yt-dlp)
ARCHIVE=~/Downloads/subtitle-archive
SKILL_DIR=~/.openclaw/workspace/skills/dirtbike-pipeline

# Feature flags (can be disabled via environment variables)
DIRTBIKE_ASR_CORRECTION=${DIRTBIKE_ASR_CORRECTION:-1}
DIRTBIKE_QUALITY_CHECK=${DIRTBIKE_QUALITY_CHECK:-1}
DIRTBIKE_WHISPER_ADVANCED=${DIRTBIKE_WHISPER_ADVANCED:-1}

# Export for subprocess access
export DIRTBIKE_ASR_CORRECTION DIRTBIKE_QUALITY_CHECK DIRTBIKE_WHISPER_ADVANCED

echo "=== Dirtbike Pipeline with ABC Optimizations ==="
echo "ASR Correction: $DIRTBIKE_ASR_CORRECTION"
echo "Quality Check: $DIRTBIKE_QUALITY_CHECK"
echo "Whisper Advanced: $DIRTBIKE_WHISPER_ADVANCED"
echo ""

echo "=== [1/6] Downloading video ==="
"$YTDLP" \
  --extractor-args 'youtube:player_client=android' \
  "$URL" \
  -f 'best[height<=1080]' \
  -o "$ARCHIVE/%(id)s-%(title)s/%(title)s.%(ext)s"

VIDEO_ID=$("$YTDLP" --extractor-args 'youtube:player_client=android' --get-id "$URL")
VIDEO_DIR=$(find "$ARCHIVE" -maxdepth 1 -type d -name "${VIDEO_ID}-*" | head -1)
[ -z "$VIDEO_DIR" ] && { echo "ERROR: Could not find downloaded directory for $VIDEO_ID"; exit 1; }
echo "Video directory: $VIDEO_DIR"

echo "=== [2/6] Whisper transcription (optimized) ==="
python3 "$SKILL_DIR/whisper_transcribe.py" "$VIDEO_DIR"

EN_SRT="$VIDEO_DIR/en.srt"
[ ! -f "$EN_SRT" ] && { echo "ERROR: en.srt not found after transcription"; exit 1; }

# ASR Correction Step (A)
if [ "$DIRTBIKE_ASR_CORRECTION" = "1" ]; then
  echo "=== [3/6] ASR correction ==="
  python3 "$SKILL_DIR/asr_corrector.py" "$EN_SRT" "$VIDEO_DIR/en_corrected.srt" --stats
  CORRECTED_SRT="$VIDEO_DIR/en_corrected.srt"
else
  echo "=== [3/6] ASR correction (skipped) ==="
  CORRECTED_SRT="$EN_SRT"
fi

echo "=== [4/6] Translating subtitles + generating title/desc ==="
# Use corrected SRT if available
if [ -f "$CORRECTED_SRT" ]; then
  # Temporarily replace en.srt for translation
  cp "$EN_SRT" "$VIDEO_DIR/en_backup.srt"
  cp "$CORRECTED_SRT" "$EN_SRT"
fi

python3 -u "$SKILL_DIR/auto_translate.py" "$VIDEO_DIR" "$URL"

# Restore original en.srt
if [ -f "$VIDEO_DIR/en_backup.srt" ]; then
  mv "$VIDEO_DIR/en_backup.srt" "$EN_SRT"
fi

ZH_SRT="$VIDEO_DIR/zh_final.srt"
[ ! -f "$ZH_SRT" ] && { echo "ERROR: zh_final.srt not found after translation"; exit 1; }

META="$VIDEO_DIR/meta.json"
TITLE=$(python3 -c "import json; d=json.load(open('$META')); print(d['title'])")
DESC=$(python3 -c "import json; d=json.load(open('$META')); print(d['desc'])")

# Quality Validation Step (C)
if [ "$DIRTBIKE_QUALITY_CHECK" = "1" ]; then
  echo "=== [5/6] Quality validation ==="
  if [ -f "$CORRECTED_SRT" ]; then
    python3 "$SKILL_DIR/quality_validator.py" "$CORRECTED_SRT" "$ZH_SRT" "$VIDEO_DIR/quality_report.json"
  else
    python3 "$SKILL_DIR/quality_validator.py" "$EN_SRT" "$ZH_SRT" "$VIDEO_DIR/quality_report.json"
  fi
else
  echo "=== [5/6] Quality validation (skipped) ==="
fi

echo "=== [6/6] Generating cover + burning subtitles ==="
python3 "$SKILL_DIR/cover_html.py" "$VIDEO_DIR" "$TITLE" --bg random

VIDEO_IN=$(find "$VIDEO_DIR" -name "*.mp4" ! -name "*_subbed*" | head -1)
[ -z "$VIDEO_IN" ] && { echo "ERROR: No source mp4 found in $VIDEO_DIR"; exit 1; }

VIDEO_SUBBED="${VIDEO_IN%.mp4}_subbed.mp4"

# Prefer explicit CJK-capable fonts to avoid missing glyph / fallback issues on macOS
if [ -f "/System/Library/Fonts/PingFang.ttc" ]; then
  FONT="PingFang SC"
elif [ -f "/System/Library/Fonts/STHeiti Medium.ttc" ]; then
  FONT="STHeiti"
else
  FONT="Arial Unicode MS"
fi

FFMPEG_FULL='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
TMP_SRT="/tmp/dirtbike_pipeline_subtitles.srt"
ln -sf "$ZH_SRT" "$TMP_SRT"

"$FFMPEG_FULL" -y -i "$VIDEO_IN" \
  -vf "subtitles=filename='$TMP_SRT':force_style='FontName=$FONT,FontSize=18,PrimaryColour=&Hffffff,OutlineColour=&H000000,BackColour=&H66000000,Outline=2,Shadow=1,MarginV=18'" \
  -c:a copy \
  "$VIDEO_SUBBED"

echo "Burned video: $VIDEO_SUBBED"

echo ""
echo "=== Uploading to Bilibili ==="
bash "$SKILL_DIR/upload.sh" "$VIDEO_DIR" "$TITLE" "$DESC" "$URL"

echo ""
echo "=== Pipeline complete ==="
echo "Directory : $VIDEO_DIR"
echo "Title     : $TITLE"
if [ "$DIRTBIKE_QUALITY_CHECK" = "1" ] && [ -f "$VIDEO_DIR/quality_report.json" ]; then
  echo "Quality   : $VIDEO_DIR/quality_report.json"
fi
