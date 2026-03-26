#!/usr/bin/env python3
"""
Auto-translate subtitles and generate Bilibili title/description.
Default: Google Translator (deep_translator). No AI API key needed.
Optional: Claude/OpenClaw backend via DIRTBIKE_MODEL_BACKEND=claude|openclaw
Usage: auto_translate.py <video_dir> [youtube_url]
Outputs: zh_final.srt, meta.json (title + desc)
"""
import sys, os, re, json, glob, subprocess, shutil
from itertools import islice

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_PATH = os.path.join(SKILL_DIR, "glossary.json")

# Default translator: "lemon" (lemonapi.site)
# Set DIRTBIKE_MODEL_BACKEND=google to use Google Translate (not recommended)
MODEL_BACKEND = os.environ.get("DIRTBIKE_MODEL_BACKEND", "lemon")
OPENCLAW_AGENT = os.environ.get("DIRTBIKE_OPENCLAW_AGENT", "main")

# Rate limiting controls (to avoid 429 Too Many Requests)
TRANSLATION_DELAY = float(os.environ.get("DIRTBIKE_TRANSLATION_DELAY", "2.0"))  # seconds between batches
BATCH_SIZE = int(os.environ.get("DIRTBIKE_BATCH_SIZE", "50"))  # subtitles per API call
MAX_RETRIES = int(os.environ.get("DIRTBIKE_MAX_RETRIES", "3"))  # retry attempts for 429 errors
RETRY_BASE_DELAY = float(os.environ.get("DIRTBIKE_RETRY_BASE_DELAY", "2.0"))  # base delay for exponential backoff

# LemonAPI 配置
LEMON_API_KEY = os.environ.get("DIRTBIKE_LEMON_API_KEY", "")
LEMON_API_BASE = os.environ.get("DIRTBIKE_LEMON_API_BASE", "https://api.moonshot.cn/v1")
LEMON_MODEL = os.environ.get("DIRTBIKE_LEMON_MODEL", "moonshot-v1-128k")

# GLM API 配置 (智谱通用翻译)
GLM_API_KEY = os.environ.get("DIRTBIKE_GLM_API_KEY", "")
GLM_API_BASE = "https://open.bigmodel.cn/api/v1/agents"
GLM_STRATEGY = "two_step"  # 两步翻译

# DeepSeek API 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")  # DeepSeek-V3.2

# Default meta backend should be stable and non-blocking for cron runs.
# If the user explicitly wants AI-generated titles/descriptions, set DIRTBIKE_META_BACKEND=lemon|claude|openclaw.
META_BACKEND = os.environ.get("DIRTBIKE_META_BACKEND", "lemon")

# Noise subtitle patterns to filter out
NOISE_RE = re.compile(
    r"^\s*[\[【].*?[\]】]\s*$|"          # [Music] 【音乐】 etc.
    r"^\s*\(.*?\)\s*$|"                   # (applause)
    r"^\s*♪.*?♪?\s*$|"                   # ♪ music ♪
    r"^\s*$",                             # blank
    re.IGNORECASE
)
NOISE_WORDS = re.compile(
    r"^\s*(uh+|um+|ah+|oh+|hmm+|huh|yeah|okay|ok|right|so|well|like|you know|i mean)\s*[,.]?\s*$",
    re.IGNORECASE
)
# Post-translation noise cleanup: remove trailing [音乐] / [Music] / ♪ etc.
POST_NOISE_RE = re.compile(r"\s*[\[【]音乐[\]】]?\s*$|\s*[\[【]Music[\]】]?\s*$|\s*♪.*?♪?\s*$", re.IGNORECASE)

# Post-translation optimization: limit length and clean punctuation
MAX_CHINESE_CHARS = 35

def post_process_subtitle(text):
    """后处理：限制长度、优化标点"""
    if not text:
        return text
    # 去除多余空格
    text = re.sub(r'\s+', ' ', text).strip()
    # 去除开头/结尾的标点
    text = re.sub(r'^[,\.\!\?\;\:]+|[,\.\!\?\;\:]+$', '', text)
    # 优化：超过长度限制时，尝试在最后一个完整句号/逗号处断开
    if len(text) > MAX_CHINESE_CHARS:
        # 找到最后一个合适的断点
        for sep in ['。', '！', '？', '，', '. ', ', ']:
            last_pos = text.rfind(sep)
            if last_pos > MAX_CHINESE_CHARS * 0.5:  # 保证断开后两边都有内容
                text = text[:last_pos + 1]
                break
    return text


def is_noise(text):
    return bool(NOISE_RE.match(text)) or bool(NOISE_WORDS.match(text))


def read_subtitle(video_dir):
    # 优先使用 Whisper 生成的 en.srt，而不是 YouTube 下载的 .en.vtt
    for pat in ["en.srt", "en_corrected.srt", "*.en.srt", "*.en.vtt", "*.vtt", "*.srt"]:
        files = glob.glob(os.path.join(video_dir, pat))
        files = [f for f in files if "zh_final" not in f]
        if files:
            return files[0]
    return None


def srt_to_entries(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\n+", content.strip())
    entries = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            entries.append({"idx": lines[0], "time": lines[1], "text": " ".join(lines[2:])})
        elif len(lines) == 2 and "-->" in lines[0]:
            entries.append({"idx": str(len(entries)+1), "time": lines[0], "text": lines[1]})
    return entries


def vtt_to_entries(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    entries = []
    blocks = re.split(r"\n\n+", content.strip())
    idx = 1
    for block in blocks:
        lines = block.strip().splitlines()
        time_line = next((l for l in lines if "-->" in l), None)
        if not time_line:
            continue
        text_lines = [
            re.sub(r"<[^>]+>", "", l).strip()
            for l in lines
            if "-->" not in l and not re.match(r"^(WEBVTT|NOTE|\d+$)", l)
        ]
        text = " ".join(t for t in text_lines if t)
        if text:
            entries.append({"idx": str(idx), "time": time_line.replace(".", ","), "text": text})
            idx += 1
    return entries


def parse_vtt_time(time_str):
    t = time_str.replace(",", ".").strip()
    parts = t.split(":")
    try:
        if len(parts) == 3:
            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = float(parts[0]), float(parts[1])
            return m * 60 + s
        return 0.0
    except (ValueError, IndexError):
        return 0.0


def get_end_time(time_str):
    parts = time_str.split("-->")
    if len(parts) == 2:
        return parse_vtt_time(parts[1].strip())
    return 0.0


def _dedup_text(text):
    """Remove obvious consecutive duplicated words/phrases introduced by merged captions."""
    words = text.split()
    if len(words) < 2:
        return text

    # First pass: drop immediate duplicated words, e.g. "the the".
    collapsed = []
    for w in words:
        if collapsed and w == collapsed[-1]:
            continue
        collapsed.append(w)

    # Second pass: collapse adjacent repeated short phrases, e.g.
    # "in the in the" -> "in the", "body position body position" -> "body position".
    out = []
    i = 0
    while i < len(collapsed):
        matched = False
        for n in (3, 2):
            if i + 2 * n <= len(collapsed) and collapsed[i:i+n] == collapsed[i+n:i+2*n]:
                out.extend(collapsed[i:i+n])
                i += 2 * n
                matched = True
                break
        if matched:
            continue
        out.append(collapsed[i])
        i += 1

    return " ".join(out)


def _remove_similar_duplicates(entries, similarity_threshold=0.7):
    """去除相似度高的相邻重复字幕"""
    if len(entries) < 2:
        return entries
    
    def similarity(a, b):
        """简单相似度计算：共同词占比"""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0
        intersection = words_a & words_b
        return len(intersection) / max(len(words_a), len(words_b))
    
    result = [entries[0]]
    for e in entries[1:]:
        prev_text = result[-1]["text"].strip()
        curr_text = e["text"].strip()
        
        # 完全相同则跳过
        if curr_text == prev_text:
            continue
        
        # 相似度高则跳过（认为是重复）
        sim = similarity(prev_text, curr_text)
        if sim >= similarity_threshold:
            continue
        
        result.append(e)
    
    return result


def merge_fragmented_entries(entries, gap_threshold=2.0, max_chars=35):
    """优化：减少合并阈值，限制单条字幕长度"""
    if not entries:
        return entries
    merged = [entries[0].copy()]

    for e in entries[1:]:
        prev_end = get_end_time(merged[-1]["time"])
        curr_start = parse_vtt_time(e["time"].split("-->")[0].strip() if "-->" in e["time"] else e["time"])
        gap = curr_start - prev_end
        prev_text = merged[-1]["text"].strip()
        curr_text = e["text"].strip()

        # 优化：检查合并后是否超过长度限制
        merged_text = prev_text + " " + curr_text
        exceed_limit = len(merged_text) > max_chars

        # 强制断句：如果当前文本以连词开头，主动拆分
        force_split = bool(re.match(r'^(and|but|so|because|which|however|therefore|then|now|well|you know|i mean)\s', curr_text, re.IGNORECASE))

        if gap < gap_threshold and not exceed_limit and not force_split:
            merged[-1]["text"] = merged[-1]["text"] + " " + curr_text
            prev_start_str = merged[-1]["time"].split("-->")[0].strip()
            merged[-1]["time"] = f"{prev_start_str} --> {e['time'].split('-->')[-1].strip()}"
        else:
            merged.append(e.copy())

    # Deduplicate text within merged entries
    for m in merged:
        m["text"] = _dedup_text(m["text"])
    return merged


def load_entries(path):
    entries = vtt_to_entries(path) if path.endswith(".vtt") else srt_to_entries(path)
    if path.endswith(".vtt"):
        entries = merge_fragmented_entries(entries, gap_threshold=3.0)
    filtered = [e for e in entries if not is_noise(e["text"])]
    deduped = []
    prev = None
    for e in filtered:
        if e["text"].strip() != prev:
            deduped.append(e)
            prev = e["text"].strip()
    return deduped


def load_glossary():
    if os.path.exists(GLOSSARY_PATH):
        try:
            with open(GLOSSARY_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"terms": []}


def merge_glossary(existing, new_terms):
    existing_en = {t[0].lower() for t in existing.get("terms", [])}
    merged = list(existing.get("terms", []))
    for pair in new_terms:
        if len(pair) == 2 and pair[0].lower() not in existing_en:
            merged.append(pair)
            existing_en.add(pair[0].lower())
    return {"terms": merged}


def save_glossary(glossary):
    with open(GLOSSARY_PATH, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2)


SUBTITLE_RULES = """你是越野摩托车翻译专家，翻译时把自己想象成一个有多年骑行经验的老手，正在给新手朋友分享心得。

【翻译风格】（核心要求）
- 用第一人称或朋友对话的口吻，像讲故事一样翻译
- 加入口语化连接词（"啊"、"呢"、"其实"、"你会发现"、"说白了"）
- 体验式叙述：多翻译出"感觉"、"你会发现"、"说白了就是"
- 技术术语保留，但解释要通俗（像给新手讲解）
- 允许适当的语气词和情感表达，不要太生硬

【语气示例】
原文："Keep your weight on the outside peg"
生硬版："保持重心在外侧脚踏"
亲切版："重心要放在外侧脚踏上啊，这个很关键" 或 "你会发现，把重心压在外侧脚踏上，车稳很多"

原文："This helps maintain balance"
生硬版："这有助于保持平衡"
亲切版："这样做啊，平衡感会好很多" 或 "说白了，就是让你心里更有底"

【硬性规则】
- 每条字幕不超过35个中文字符
- 技术术语必须准确（保留术语表词汇）
- 主动断句：长句从连词（and/but/so/because/which）处拆分
- 保留原序号和时间轴不变
- 噪音字幕（【音乐】【掌声】等）直接跳过或输出省略号

【核心术语表·必须严格使用】
KTM→KTM（保持原样）, Yamaha→雅马哈, Honda→本田, Suzuki→铃木, Kawasaki→川崎,
MX→越野摩托, Supercross→室内超级越野, Motocross→越野摩托, Enduro→耐力赛, GNCC→GNCC越野赛,
Pro Motocross→职业越野, Amateur→业余组, Pro→职业组, Vet→老将组,
450cc→450cc, 250cc→250cc, 125cc→125cc, two-stroke→二冲程, four-stroke→四冲程,
rut→车辙, rutted→车辙化的, off-camber→侧倾弯/倾斜路面,
look ahead→向前看, looking ahead→注视前方, head turning→转头技巧,
whoops→连续起伏, rhythm section→节奏路段, rhythm lane→节奏线,
triple→三连跳, double→双跳, single→单跳,
tabletop/table-top→平顶跳, face of jump→跳台正面, landing→着陆区, launch→发射区,
berm→弯道外倾, flat corner→平缓弯, inside line→内线, outside line→外线, preferred line→最佳线路,
corner→弯道, corners→弯道（复数）,
technical section→技术路段, simple section→简单路段,
stutter bump→减速坎, G-out→重力弯, rollers→波浪路, hay bale→草堆,
starting gate→起步门, start→起步/发车, gate drop→发车跳下,
holeshot→首位领跑, holeshot award→首位奖, clean start→干净起步, bad start→起步失误,
moto→越野赛, main event→主赛, heat race→预赛, LCQ→补赛,
moto score→单场得分, overall→总成绩, pole position→杆位, fast qualifier→最快排位,
sekonda→第二圈, final lap→最后一圈, winner→冠军, podium→领奖台,
DNS→未发车, DNF→未完赛, DNQ→未晋级,
Luke Fauser→Luke Fauser（人名不翻译）, riders→车手, bike→赛车, track→赛道
"""

def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]


# ─── Google Translator (default, no API key) ───

import threading, concurrent.futures


def _translate_with_timeout(translator, texts, timeout_sec):
    def _run():
        return translator.translate_batch(texts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run)
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            return None


def deepl_translate_chunk(chunk):
    import json as _json, requests as _req
    api_key = os.environ.get("DEEPL_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPL_API_KEY not set")
    texts = [e["text"] for e in chunk]
    r = _req.post(
        "https://api-free.deepl.com/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        json={"text": texts, "target_lang": "ZH", "source_lang": "EN"},
        timeout=30,
    )
    r.raise_for_status()
    translated = [t["text"] for t in r.json()["translations"]]
    return [{**src, "text": txt} for src, txt in zip(chunk, translated)]


def _parse_glm_response(response_text, original_chunk):
    """解析 GLM 返回的翻译结果，对齐到原始字幕
    GLM 返回 SRT 格式，我们按顺序对应原始条目
    """
    lines = [l.strip() for l in response_text.strip().split("\n") if l.strip()]

    # 提取所有翻译文本（跳过序号和时间轴）
    translated_texts = []
    i = 0
    while i < len(lines):
        if lines[i].isdigit():
            i += 1  # 跳过序号
            if i < len(lines) and "-->" in lines[i]:
                i += 1  # 跳过时间轴
            if i < len(lines) and not lines[i].isdigit():
                translated_texts.append(lines[i])
                i += 1
            else:
                translated_texts.append("")
        else:
            i += 1

    # 按顺序对应原始条目
    result = []
    for i, orig in enumerate(original_chunk):
        if i < len(translated_texts) and translated_texts[i].strip():
            result.append({
                "idx": orig["idx"],
                "time": orig["time"],
                "text": translated_texts[i].strip()
            })
        else:
            result.append({
                "idx": orig["idx"],
                "time": orig["time"],
                "text": orig["text"]
            })

    return result


def glm_translate_chunk(chunk, glossary_terms=None, prev_entries=None):
    """GLM 通用翻译 API (智谱) - 批量一次性翻译所有字幕"""
    import requests

    # 构建 SRT 格式文本，一次性发送
    # 注意：原始字幕索引可能不连续，我们用自然顺序 1,2,3... 发送
    srt_lines = []
    for i, e in enumerate(chunk, 1):
        text = e["text"].strip()
        srt_lines.append(f"{i}\n{e['time']}\n{text}\n")

    srt_text = "\n".join(srt_lines)

    try:
        resp = requests.post(
            GLM_API_BASE,
            headers={
                "Authorization": f"Bearer {GLM_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "agent_id": "general_translation",
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": srt_text}]
                    }
                ],
                "custom_variables": {
                    "source_lang": "en",
                    "target_lang": "zh-CN",
                    "strategy": GLM_STRATEGY
                }
            },
            timeout=180
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("status") == "success" and result.get("choices"):
            choice = result["choices"][0]
            messages = choice.get("messages", [])
            for msg in messages:
                if msg.get("role") == "assistant":
                    content = msg.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        translated_text = content.get("text", "").strip()
                        return _parse_glm_response(translated_text, chunk)
                    elif isinstance(content, str):
                        return _parse_glm_response(content.strip(), chunk)
            else:
                # Fallback: try message.content directly
                msg_content = choice.get("message", {}).get("content", {})
                if isinstance(msg_content, dict) and msg_content.get("text"):
                    return _parse_glm_response(msg_content["text"].strip(), chunk)
                elif isinstance(msg_content, str):
                    return _parse_glm_response(msg_content.strip(), chunk)

        print(f"WARNING: GLM API error: {result.get('error', result)}", file=sys.stderr)
    except Exception as ex:
        print(f"WARNING: GLM batch failed: {ex}", file=sys.stderr)

    # Fallback: return original
    return [{"idx": e["idx"], "time": e["time"], "text": e["text"]} for e in chunk]


def lemon_translate_chunk(chunk, glossary_terms=None, prev_entries=None):
    """LemonAPI 翻译 (OpenAI 兼容格式)，带上下文语境和限流控制"""
    import requests
    import time
    glossary_json = glossary_terms or "[]"

    # 构建上文语境
    context_text = ""
    if prev_entries:
        context_text = "【上文语境】（翻译时保持术语一致）：\n"
        context_text += "\n".join(
            f"{e['idx']}|{e['time']}|{e['text']}"
            for e in prev_entries[-3:]
        )
        context_text += "\n\n"

    out = []
    for batch in chunked(chunk, BATCH_SIZE):  # Use configurable batch size
        texts = [e["text"] for e in batch]
        # 构建 prompt（使用改进的翻译规则）
        prompt = f"""{context_text}请把下面字幕翻译成中文。
输入格式：每行 `序号|时间轴|英文原文`
输出格式：每行 `序号|时间轴|中文译文`
不要输出解释，不要输出 markdown 代码块，只输出结果行。

{SUBTITLE_RULES}

术语表（优先使用）：{glossary_json}

{chr(10).join(f"{e['idx']}|{e['time']}|{e['text']}" for e in batch)}
"""
        # 限流：请求前延迟
        if TRANSLATION_DELAY > 0:
            time.sleep(TRANSLATION_DELAY)

        # 带重试的翻译请求
        batch_out = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{LEMON_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {LEMON_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": LEMON_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 4000
                    },
                    timeout=60
                )
                resp.raise_for_status()
                result = resp.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                # 解析翻译结果（每行一个翻译，格式：idx|time|text）
                lines = [l.strip() for l in content.split("\n") if l.strip()]

                # 匹配翻译结果到原始字幕
                batch_out = []
                for i, src in enumerate(batch):
                    if i < len(lines):
                        parts = lines[i].split("|")
                        # 取最后一段作为译文（可能有 align:start 等后缀）
                        zh = parts[-1].strip() if len(parts) >= 2 else lines[i].strip()
                        # 去除可能的 align 后缀
                        zh = re.sub(r'\s+align:start.*$', '', zh).strip()
                    else:
                        zh = src["text"]
                    zh = re.sub(r"\s+", " ", zh).strip()
                    batch_out.append({"idx": src["idx"], "time": src["time"], "text": zh})
                break  # 成功，跳出重试循环
            except Exception as e:
                error_msg = str(e)
                is_rate_limit = "429" in error_msg or "Too Many Requests" in error_msg
                if is_rate_limit and attempt < MAX_RETRIES - 1:
                    # 指数退避：2s, 4s, 8s...
                    retry_delay = RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"WARNING: Rate limited (429), retrying in {retry_delay}s... (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(retry_delay)
                    continue
                else:
                    print(f"WARNING: LemonAPI batch failed after {attempt + 1} attempts: {e}", file=sys.stderr)
                    # Fallback: 保留原文
                    batch_out = [{"idx": src["idx"], "time": src["time"], "text": src["text"]} for src in batch]
                    break
        out.extend(batch_out)
    return out


def google_translate_chunk(chunk):
    try:
        from deep_translator import GoogleTranslator
    except Exception as e:
        raise RuntimeError(f"deep_translator not installed: {e}")
    translator = GoogleTranslator(source="auto", target="zh-CN")
    out = []
    for batch in chunked(chunk, 10):
        texts = [e["text"] for e in batch]
        translated = _translate_with_timeout(translator, texts, 30)
        if translated is None:
            # Fallback: translate one by one, skip on failure
            translated = []
            for t in texts:
                try:
                    result = _translate_with_timeout(translator, [t], 10)
                    translated.append(result[0] if result else t)
                except Exception:
                    translated.append(t)
        for src, txt in zip(batch, translated):
            zh = (txt or src["text"]).strip()
            zh = re.sub(r"\s+", " ", zh)
            out.append({"idx": src["idx"], "time": src["time"], "text": zh})
    return out


def deepseek_translate_chunk(chunk, glossary_terms=None, prev_entries=None):
    """DeepSeek API 翻译 (OpenAI 兼容格式)，使用 DeepSeek-V3.2 模型"""
    import requests
    import time
    glossary_json = glossary_terms or "[]"

    # 构建上文语境
    context_text = ""
    if prev_entries:
        context_text = "【上文语境】（翻译时保持术语一致）：\n"
        context_text += "\n".join(
            f"{e['idx']}|{e['time']}|{e['text']}"
            for e in prev_entries[-3:]
        )
        context_text += "\n\n"

    out = []
    for batch in chunked(chunk, BATCH_SIZE):
        texts = [e["text"] for e in batch]
        # 构建 prompt（使用改进的翻译规则）
        prompt = f"""{context_text}请把下面字幕翻译成中文。
输入格式：每行 `序号|时间轴|英文原文`
输出格式：每行 `序号|时间轴|中文译文`
不要输出解释，不要输出 markdown 代码块，只输出结果行。

{SUBTITLE_RULES}

术语表（优先使用）：{glossary_json}

{chr(10).join(f"{e['idx']}|{e['time']}|{e['text']}" for e in batch)}
"""
        # 限流：请求前延迟
        if TRANSLATION_DELAY > 0:
            time.sleep(TRANSLATION_DELAY)

        # 带重试的翻译请求
        batch_out = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 4000
                    },
                    timeout=60
                )
                resp.raise_for_status()
                result = resp.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                # 解析翻译结果（每行一个翻译，格式：idx|time|text）
                lines = [l.strip() for l in content.split("\n") if l.strip()]

                # 匹配翻译结果到原始字幕
                batch_out = []
                for i, src in enumerate(batch):
                    if i < len(lines):
                        parts = lines[i].split("|")
                        # 取最后一段作为译文（可能有 align:start 等后缀）
                        zh = parts[-1].strip() if len(parts) >= 2 else lines[i].strip()
                        # 去除可能的 align 后缀
                        zh = re.sub(r'\s+align:start.*$', '', zh).strip()
                    else:
                        zh = src["text"]
                    zh = re.sub(r"\s+", " ", zh).strip()
                    batch_out.append({"idx": src["idx"], "time": src["time"], "text": zh})
                break  # 成功，跳出重试循环
            except Exception as e:
                error_msg = str(e)
                is_rate_limit = "429" in error_msg or "Too Many Requests" in error_msg
                if is_rate_limit and attempt < MAX_RETRIES - 1:
                    # 指数退避：2s, 4s, 8s...
                    retry_delay = RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"WARNING: Rate limited (429), retrying in {retry_delay}s... (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(retry_delay)
                    continue
                else:
                    print(f"WARNING: DeepSeek API batch failed after {attempt + 1} attempts: {e}", file=sys.stderr)
                    # Fallback: 保留原文
                    batch_out = [{"idx": src["idx"], "time": src["time"], "text": src["text"]} for src in batch]
                    break
        out.extend(batch_out)
    return out


# ─── AI backends (claude / openclaw) ───

AI_DISABLED = False


def run_ai(prompt, backend_override=None):
    global AI_DISABLED
    if AI_DISABLED:
        raise RuntimeError("AI backend disabled for this run")

    backend = (backend_override or MODEL_BACKEND or "google").lower().strip()
    if backend == "google":
        raise RuntimeError("AI not available when backend=google")

    # LemonAPI backend
    if backend == "lemon":
        import requests
        try:
            resp = requests.post(
                f"{LEMON_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LEMON_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LEMON_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 4000
                },
                timeout=60
            )
            resp.raise_for_status()
            result = resp.json()
            return result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            if "quota" in str(e).lower() or "credit" in str(e).lower() or "额度" in str(e):
                AI_DISABLED = True
            raise RuntimeError(f"lemon API failed: {e}")

    if backend == "openclaw":
        openclaw = shutil.which("openclaw")
        if not openclaw:
            AI_DISABLED = True
            raise RuntimeError("openclaw CLI not found")
        proc = subprocess.run(
            ["openclaw", "agent", "--agent", OPENCLAW_AGENT, "--local", "--json", "--timeout", "300", "--message", prompt],
            capture_output=True, text=True, timeout=120,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        msg = out or err
        if proc.returncode != 0:
            if any(x in msg.lower() for x in ["authenticate", "quota", "403", "insufficient", "credit"]) or "额度" in msg:
                AI_DISABLED = True
            raise RuntimeError(f"openclaw rc={proc.returncode}: {msg[:500]}")
        try:
            obj = json.loads(out)
            payloads = obj.get("payloads") or []
            if payloads and isinstance(payloads[0], dict) and "text" in payloads[0]:
                return (payloads[0]["text"] or "").strip()
        except Exception:
            pass
        raise RuntimeError("failed to parse openclaw output")

    # claude
    claude = shutil.which("claude")
    if not claude:
        AI_DISABLED = True
        raise RuntimeError("claude CLI not found")
    proc = subprocess.run(
        [claude, "--permission-mode", "bypassPermissions", "--print", prompt],
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    msg = out or err
    if proc.returncode != 0:
        if any(x in msg.lower() for x in ["authenticate", "quota", "403", "insufficient", "credit"]) or "额度" in msg:
            AI_DISABLED = True
        raise RuntimeError(f"claude rc={proc.returncode}: {msg[:500]}")
    return out


def ai_translate_chunk(chunk, glossary_terms, prev_entries=None):
    """翻译字幕chunk，带上文语境"""
    # 构建上文语境
    context_text = ""
    if prev_entries:
        context_text = "【上文语境】（翻译时保持术语一致）：\n"
        context_text += "\n".join(
            f"{e['idx']}|{e['time']}|{e['text']}"
            for e in prev_entries[-3:]
        )
        context_text += "\n\n"

    chunk_text = "\n".join(
        f"{e['idx']}|{e['time']}|{e['text']}"
        for e in chunk
    )

    prompt = f"""{context_text}请把下面字幕翻译成中文。
输入格式：每行 `序号|时间轴|英文原文`
输出格式：每行 `序号|时间轴|中文译文`
不要输出解释，不要输出 markdown 代码块，只输出结果行。

{SUBTITLE_RULES}

术语表（优先使用）：{glossary_terms}

{chunk_text}
"""
    return run_ai(prompt)


def repair_chunk_alignment(chunk, result):
    parsed = []
    for line in result.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            parsed.append({"idx": parts[0].strip(), "time": parts[1].strip(), "text": parts[2].strip()})
    if len(parsed) == len(chunk):
        return parsed
    translated = []
    for src, out in zip(chunk, parsed):
        translated.append({"idx": src["idx"], "time": src["time"], "text": out.get("text", src["text"])})
    for src in chunk[len(translated):]:
        translated.append({"idx": src["idx"], "time": src["time"], "text": src["text"]})
    return translated


# ─── Glossary apply ───

def apply_glossary(text, glossary_terms):
    fixed = text
    for en, zh in glossary_terms:
        if not en or not zh:
            continue
        fixed = re.sub(rf"\b{re.escape(en)}\b", zh, fixed, flags=re.IGNORECASE)
    # Common post-translation fixes
    fixed = fixed.replace("二冲程", "二冲").replace("摩托十字", "越野摩托车")
    fixed = fixed.replace("快乐谷", "欢乐谷（赛道名）")
    fixed = fixed.replace("越野摩托车越野", "越野摩托车")
    return fixed


# ─── Glossary extraction (optional AI) ───

def extract_glossary(sample_text):
    prompt = f"""从以下越野摩托车视频字幕里提取专有名词、品牌、技术术语。
输出 JSON：{{"terms":[["英文","中文"],...]}}
不要输出别的内容。

{sample_text[:3000]}
"""
    try:
        text = run_ai(prompt)
        m = re.search(r"\{[\s\S]*\}", text)
        return json.loads(m.group() if m else text)
    except Exception:
        return {"terms": []}


# ─── Meta generation ───

def _normalize_generated_title(title):
    title = (title or "").strip()
    title = re.sub(r"[\s._-]+$", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _fallback_translate_title(raw_title):
    raw = (raw_title or "").strip()
    if not raw:
        return "越野摩托车技术解析"

    # Phrase-level replacements first, then single words.
    phrase_map = [
        ("Standing Position", "站姿技巧"),
        ("Attack Position", "攻击姿态"),
        ("Seated Position", "坐姿控制"),
        ("Body Positioning", "身体姿态"),
        ("Body Position", "身体姿态"),
        ("Race Breakdown", "比赛拆解"),
        ("Where to Look", "视线引导"),
        ("Corner Control", "弯道控制"),
        ("Corner Speed", "弯道速度"),
        ("Corner Entry", "入弯技巧"),
        ("Corner Technique", "弯道技术"),
        ("Jump Technique", "跳跃技术"),
        ("Jump Training", "跳跃训练"),
        ("Start Technique", "起步技术"),
        ("Start Training", "起步训练"),
        ("Brake Control", "刹车控制"),
        ("Rut Technique", "车辙技术"),
        ("Rhythm Section", "节奏段"),
        ("Whoops Technique", "起伏地形技巧"),
    ]
    word_map = [
        ("Motocross", "越野摩托"),
        ("Supercross", "室内超级越野"),
        ("Corner", "弯道"),
        ("Jump", "跳跃"),
        ("Brake", "刹车"),
        ("Start", "起步"),
        ("Technique", "技巧"),
        ("Training", "训练"),
        ("Tips", "技巧"),
        ("Control", "控制"),
        ("Position", "姿态"),
        ("Standing", "站姿"),
        ("Seat", "坐姿"),
        ("Seated", "坐姿"),
        ("Attack", "攻击"),
        ("with", "与"),
    ]

    title = raw
    for en, zh in phrase_map:
        title = re.sub(re.escape(en), zh, title, flags=re.IGNORECASE)
    for en, zh in word_map:
        title = re.sub(rf"\b{re.escape(en)}\b", zh, title, flags=re.IGNORECASE)

    title = title.replace("：", " ").replace(":", " ")
    title = re.sub(r"\b(with|and|the|a|an|of|for|to|in|on)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    # If English still remains, ask Google once more on the raw title, then clean again.
    if re.search(r"[A-Za-z]{3,}", title):
        try:
            from deep_translator import GoogleTranslator
            gt = GoogleTranslator(source="auto", target="zh-CN")
            translated = (gt.translate(raw) or "").strip()
            if translated:
                title = translated
        except Exception:
            pass
        title = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip()

    # Remove any leftover ASCII words/fragments after translation attempts.
    title = re.sub(r"\b[A-Za-z]+\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    title = _normalize_generated_title(title)

    if not title:
        return "越野摩托车技术解析"
    return title[:20]


def generate_meta(zh_srt_text, youtube_url, video_title=""):
    # Try AI-generated meta first. Even when translation backend is google,
    # we still prefer AI for stronger title/description generation if available.
    if not AI_DISABLED:
        prompt = f"""你是越野摩托车内容运营专家。请根据以下中文字幕内容，为 B 站转载视频生成高质量投稿文案。

【标题要求】（严格遵守）
- 基于"原始标题 + 字幕核心内容"重写
- 突出比赛看点/车手/组别/赛道/技术点/车型
- 禁止空泛泛泛（如"越野摩托车精彩视频"）
- 20字以内
- 优先使用：动词+技术点/车型+效果 的组合
- 示例：""KTM战车入弯攻略"", "270cc公开组起步分析", "职业车手过弯姿态拆解"

【简介要求】（严格遵守）
- 口语化、有信息密度，像看完视频后写的
- 开头注明"本视频搬运自 YouTube 原博主，版权归原作者所有"
- 中间交代：视频主题、适合谁看、关键技术点/实战价值
- 不要空话套话、泛泛夸赞
- 不要重复贴原视频链接
- 结尾自然引导关注
- 目标500字左右，明显少于400字不合格

【风格】
- 速度感、技术感
- 专业但易懂
- 有干货、不水

中文字幕内容（节选）：
{zh_srt_text[:4000]}

原视频标题（重点参考）：{video_title}

输出 JSON：{{"title":"...","desc":"..."}}
只输出 JSON，不要输出其他任何内容。
"""
        try:
            text = run_ai(prompt, backend_override=META_BACKEND)
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                obj = json.loads(m.group())
                title = _normalize_generated_title(obj.get("title", ""))
                desc = obj.get("desc", "")
                if title and len(title) > 3 and not re.search(r"[A-Za-z]{4,}", title):
                    return {"title": title[:20], "desc": desc}
        except Exception as e:
            if (META_BACKEND or "google").lower().strip() != "google":
                print(f"WARNING: AI meta failed: {e}", file=sys.stderr)

    # Rule-based fallback: translate the full title
    title = _fallback_translate_title(video_title)
    title_base = title

    # Build a decent description from the video title
    desc = (
        "本视频搬运自 YouTube 原博主，版权归原作者所有。\n\n"
        f"这期内容围绕{title_base[:50] if title_base else '越野摩托车技术与比赛实战'}展开，不是单纯看热闹，而是能反复琢磨动作细节的一类视频。"
        "如果你平时一到弯道、起伏地形、线路变化或者节奏切换就容易发懵，这种国外一线骑手和教练视角的视频就很有参考价值。"
        "它最大的价值，不只是告诉你高手快，而是能让你看到高手到底快在哪里、稳在哪里、失误是怎么避免的。\n\n"
        "结合字幕内容来看，视频里通常会围绕线路选择、身体姿态、油门与刹车配合、入弯到出弯的节奏、不同地形下的处理逻辑来展开。"
        "对刚入门的人来说，可以先看大思路；对已经在骑的人来说，更值得盯住每一个动作背后的原因：为什么这里要提前准备、为什么这条线更顺、为什么有些看似快的做法反而会拖慢下一段节奏。"
        "这类内容最适合一边看一边代入自己平时骑车的问题。\n\n"
        "如果你也喜欢这种有比赛画面、有技术拆解、能真正提升骑行理解的视频，记得关注我。后面我会继续搬运更多国外优质越野摩托车内容，把能落地的技巧、常见误区和实战经验持续整理出来。"
    )
    return {"title": title, "desc": desc}


def entries_to_srt(entries):
    lines = []
    for e in entries:
        lines.append(str(e["idx"]))
        lines.append(e["time"])
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)


# ─── Main ───

def main():
    backend = (MODEL_BACKEND or "google").lower().strip()
    print(f"Translation backend: {backend}")

    if len(sys.argv) < 2:
        print("Usage: auto_translate.py <video_dir> [youtube_url]")
        sys.exit(1)

    video_dir = sys.argv[1]
    youtube_url = sys.argv[2] if len(sys.argv) > 2 else ""

    sub_path = read_subtitle(video_dir)
    if not sub_path:
        print("ERROR: No subtitle file found", file=sys.stderr)
        sys.exit(1)
    print(f"Subtitle: {sub_path}")

    entries = load_entries(sub_path)
    if not entries:
        print("ERROR: No subtitle entries parsed", file=sys.stderr)
        sys.exit(1)
    print(f"Entries after noise filter: {len(entries)}")

    # Load existing glossary
    merged_glossary = load_glossary()
    # Optional: AI glossary extraction
    if backend != "google" and not AI_DISABLED:
        sample = "\n".join(e["text"] for e in entries[:50])
        try:
            new_glossary = extract_glossary(sample)
            merged_glossary = merge_glossary(merged_glossary, new_glossary.get("terms", []))
            save_glossary(merged_glossary)
        except Exception as e:
            print(f"WARNING: glossary extraction skipped: {e}", file=sys.stderr)
    glossary_json = json.dumps(merged_glossary.get("terms", []), ensure_ascii=False)
    print(f"Glossary: {len(merged_glossary.get('terms', []))} terms (persistent)")

    # Translate all chunks
    chunk_size = 30
    translated = []
    prev_entries = []  # 保存上文条目用于语境连贯
    total = (len(entries) + chunk_size - 1) // chunk_size
    for i in range(0, len(entries), chunk_size):
        chunk = entries[i:i+chunk_size]
        print(f"Translating chunk {i//chunk_size + 1}/{total}...")

        if backend == "deepl":
            chunk_out = deepl_translate_chunk(chunk)
        elif backend == "glm":
            chunk_out = glm_translate_chunk(chunk, glossary_json, prev_entries)
        elif backend == "lemon":
            chunk_out = lemon_translate_chunk(chunk, glossary_json, prev_entries)
        elif backend == "deepseek":
            chunk_out = deepseek_translate_chunk(chunk, glossary_json, prev_entries)
        elif backend == "google":
            chunk_out = google_translate_chunk(chunk)
        else:
            try:
                result = ai_translate_chunk(chunk, glossary_json, prev_entries)
                chunk_out = repair_chunk_alignment(chunk, result)
            except Exception as e:
                print(f"WARNING: AI failed, fallback to LemonAPI: {e}", file=sys.stderr)
                chunk_out = lemon_translate_chunk(chunk, glossary_json, prev_entries)

        # 更新上文语境（保留最近3条）
        if chunk_out:
            prev_entries = chunk_out[-3:]

        for item in chunk_out:
            item["text"] = apply_glossary(item["text"], merged_glossary.get("terms", []))
            # Post-translation noise cleanup
            item["text"] = POST_NOISE_RE.sub("", item["text"]).strip()
            # Post-process: limit length and clean punctuation
            item["text"] = post_process_subtitle(item["text"])
            if not item["text"]:
                item["text"] = "..."  # placeholder to avoid empty subtitle
        translated.extend(chunk_out)

    # 相似度去重：去除相邻的重复/相似字幕
    translated = _remove_similar_duplicates(translated, similarity_threshold=0.6)
    print(f"Entries after similarity dedup: {len(translated)}")

    out_srt = os.path.join(video_dir, "zh_final.srt")
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write(entries_to_srt(translated))
    print(f"Written: {out_srt}")

    zh_text = "\n".join(e["text"] for e in translated)
    dir_name = os.path.basename(video_dir)
    # video_dir is usually "<youtube_id>-<title>", but YouTube IDs themselves can contain '-'.
    # Strip exactly one leading 11-char ID plus the following dash, instead of splitting on the first dash.
    video_title = re.sub(r"^[A-Za-z0-9_-]{11}-", "", dir_name).strip()
    meta = generate_meta(zh_text, youtube_url, video_title)
    out_meta = os.path.join(video_dir, "meta.json")
    with open(out_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"Written: {out_meta}")
    print(f"Title: {meta['title']}")


if __name__ == "__main__":
    main()
