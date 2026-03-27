#!/usr/bin/env python3
"""
Auto-translate subtitles and generate Bilibili title/description.
Default: Google Translator (deep_translator). No AI API key needed.
Optional: Claude/OpenClaw backend via DIRTBIKE_MODEL_BACKEND=claude|openclaw

Usage: auto_translate.py <video_dir> [youtube_url] [--source-srt <path>]

Outputs: zh_final.srt, meta.json (title + desc)
"""

import sys, os, re, json, glob, subprocess, shutil, argparse
from itertools import islice
from pathlib import Path

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_PATH = os.path.join(SKILL_DIR, "glossary.json")

# --- .env 支持：优先读 SKILL_DIR/.env，方便本地配置 ---
_env_file = Path(SKILL_DIR) / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Default translator: "lemon" (lemonapi.site / moonshot)
MODEL_BACKEND = os.environ.get("DIRTBIKE_MODEL_BACKEND", "lemon")
OPENCLAW_AGENT = os.environ.get("DIRTBIKE_OPENCLAW_AGENT", "main")

# Rate limiting controls
TRANSLATION_DELAY  = float(os.environ.get("DIRTBIKE_TRANSLATION_DELAY",  "2.0"))
BATCH_SIZE         = int(os.environ.get("DIRTBIKE_BATCH_SIZE",           "50"))
MAX_RETRIES        = int(os.environ.get("DIRTBIKE_MAX_RETRIES",          "3"))
RETRY_BASE_DELAY   = float(os.environ.get("DIRTBIKE_RETRY_BASE_DELAY",  "2.0"))

# LemonAPI 配置
LEMON_API_KEY  = os.environ.get("DIRTBIKE_LEMON_API_KEY",  "")
LEMON_API_BASE = os.environ.get("DIRTBIKE_LEMON_API_BASE", "https://api.moonshot.cn/v1")
LEMON_MODEL    = os.environ.get("DIRTBIKE_LEMON_MODEL",    "moonshot-v1-128k")

# DeepSeek API 配置
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY",   "")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE",  "https://api.deepseek.com/v1")
DEEPSEEK_MODEL    = os.environ.get("DEEPSEEK_MODEL",     "deepseek-chat")

META_BACKEND = os.environ.get("DIRTBIKE_META_BACKEND", "lemon")

# Noise subtitle patterns
NOISE_RE = re.compile(
    r"^\s*[\[【].*?[\]】]\s*$|"
    r"^\s*\(.*?\)\s*$|"
    r"^\s*♪.*?♪?\s*$|"
    r"^\s*$",
    re.IGNORECASE
)
NOISE_WORDS = re.compile(
    r"^\s*(uh+|um+|ah+|oh+|hmm+|huh|yeah|okay|ok|right|so|well|like|you know|i mean)\s*[,.]?\s*$",
    re.IGNORECASE
)
POST_NOISE_RE = re.compile(r"\s*[\[【]音乐[\]】]?\s*$|\s*[\[【]Music[\]】]?\s*$|\s*♪.*?♪?\s*$", re.IGNORECASE)
MAX_CHINESE_CHARS = 35


def post_process_subtitle(text):
    if not text:
        return text
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[,.!?;:]+|[,.!?;:]+$', '', text)
    if len(text) > MAX_CHINESE_CHARS:
        for sep in ['。', '！', '？', '，', '. ', ', ']:
            last_pos = text.rfind(sep)
            if last_pos > MAX_CHINESE_CHARS * 0.5:
                text = text[:last_pos + 1]
                break
    return text


def is_noise(text):
    return bool(NOISE_RE.match(text)) or bool(NOISE_WORDS.match(text))


def read_subtitle(video_dir):
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
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return 0.0
    except (ValueError, IndexError):
        return 0.0


def get_end_time(time_str):
    parts = time_str.split("-->")
    return parse_vtt_time(parts[1].strip()) if len(parts) == 2 else 0.0


def _dedup_text(text):
    words = text.split()
    if len(words) < 2:
        return text
    collapsed = []
    for w in words:
        if collapsed and w == collapsed[-1]:
            continue
        collapsed.append(w)
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
    if len(entries) < 2:
        return entries
    def similarity(a, b):
        wa, wb = set(a.lower().split()), set(b.lower().split())
        if not wa or not wb:
            return 0
        return len(wa & wb) / max(len(wa), len(wb))
    result = [entries[0]]
    for e in entries[1:]:
        prev, curr = result[-1]["text"].strip(), e["text"].strip()
        if curr == prev or similarity(prev, curr) >= similarity_threshold:
            continue
        result.append(e)
    return result


def merge_fragmented_entries(entries, gap_threshold=1.5, max_chars=35):
    """合并碎片字幕。gap_threshold 降至 1.5s，减少无关句子被合并。"""
    if not entries:
        return entries
    merged = [entries[0].copy()]
    for e in entries[1:]:
        prev_end   = get_end_time(merged[-1]["time"])
        curr_start = parse_vtt_time(e["time"].split("-->")[0].strip() if "-->" in e["time"] else e["time"])
        gap = curr_start - prev_end
        prev_text, curr_text = merged[-1]["text"].strip(), e["text"].strip()
        merged_text  = prev_text + " " + curr_text
        exceed_limit = len(merged_text) > max_chars
        force_split  = bool(re.match(
            r'^(and|but|so|because|which|however|therefore|then|now|well|you know|i mean)\s',
            curr_text, re.IGNORECASE
        ))
        if gap < gap_threshold and not exceed_limit and not force_split:
            merged[-1]["text"] = merged_text
            start_str = merged[-1]["time"].split("-->")[0].strip()
            merged[-1]["time"] = f"{start_str} --> {e['time'].split('-->')[-1].strip()}"
        else:
            merged.append(e.copy())
    for m in merged:
        m["text"] = _dedup_text(m["text"])
    return merged


def load_entries(path):
    entries = vtt_to_entries(path) if path.endswith(".vtt") else srt_to_entries(path)
    if path.endswith(".vtt"):
        entries = merge_fragmented_entries(entries, gap_threshold=3.0)
    filtered = [e for e in entries if not is_noise(e["text"])]
    deduped, prev = [], None
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


# --- 断点续传缓存 ---
def load_cache(video_dir):
    cache_path = os.path.join(video_dir, ".translate_cache.json")
    if os.path.exists(cache_path):
        try:
            return json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(video_dir, cache):
    cache_path = os.path.join(video_dir, ".translate_cache.json")
    try:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as ex:
        print(f"WARNING: cache save failed: {ex}", file=sys.stderr)


SUBTITLE_RULES = """你是越野摩托车翻译专家，翻译时把自己想象成一个有多年骑行经验的老手，正在给新手朋友分享心得。

【翻译风格】（核心要求）
- 用第一人称或朋友对话的口吻，像讲故事一样翻译
- 加入口语化连接词（"啊"、"呢"、"其实"、"你会发现"、"说白了"）
- 体验式叙述：多翻译出"感觉"、"你会发现"、"说白了就是"
- 技术术语保留，但解释要通俗（像给新手讲解）
- 允许适当的语气词和情感表达，不要太生硬

【硬性规则】
- 每条字幕不超过35个中文字符
- 技术术语必须准确（保留术语表词汇）
- 主动断句：长句从连词（and/but/so/because/which）处拆分
- 保留原序号和时间轴不变
- 噪音字幕（【音乐】【掌声】等）直接跳过或输出省略号

【核心术语表·必须严格使用】
KTM→KTM, Yamaha→雅马哈, Honda→本田, Suzuki→铃木, Kawasaki→川崎,
MX→越野摩托, Supercross→室内超级越野, Motocross→越野摩托, Enduro→耐力赛,
whoops→连续起伏, rhythm section→节奏路段, triple→三连跳, double→双跳,
tabletop→平顶跳, berm→弯道外倾, flat corner→平缓弯,
holeshot→首位领跑, moto→越野赛, main event→主赛, heat race→预赛,
two-stroke→二冲程, four-stroke→四冲程, rut→车辙, off-camber→侧倾弯,
look ahead→向前看, riders→车手, bike→赛车, track→赛道
"""


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]


# =============================================================================
# 核心重构：通用 OpenAI 兼容翻译函数
# lemon / deepseek 共用同一套逻辑，只有 api_base / api_key / model 不同
# =============================================================================

def _build_translate_prompt(batch, glossary_terms, prev_entries):
    """构建翻译 prompt，带上文语境。"""
    context_text = ""
    if prev_entries:
        context_text = "【上文语境】（翻译时保持术语一致）：\n"
        context_text += "\n".join(
            f"{e['idx']}|{e['time']}|{e['text']}"
            for e in prev_entries[-3:]
        )
        context_text += "\n\n"
    glossary_json = glossary_terms or "[]"
    lines = "\n".join(f"{e['idx']}|{e['time']}|{e['text']}" for e in batch)
    return f"""{context_text}请把下面字幕翻译成中文。
输入格式：每行 `序号|时间轴|英文原文`
输出格式：每行 `序号|时间轴|中文译文`
不要输出解释，不要输出 markdown 代码块，只输出结果行。
{SUBTITLE_RULES}
术语表（优先使用）：{glossary_json}
{lines}
"""


def _parse_translate_response(content, batch):
    """解析 LLM 返回的翻译结果，对齐到原始 batch。"""
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    result = []
    for i, src in enumerate(batch):
        if i < len(lines):
            parts = lines[i].split("|")
            zh = parts[-1].strip() if len(parts) >= 2 else lines[i].strip()
            zh = re.sub(r'\s+align:start.*$', '', zh).strip()
        else:
            zh = src["text"]
        zh = re.sub(r"\s+", " ", zh).strip()
        result.append({"idx": src["idx"], "time": src["time"], "text": zh})
    return result


def _openai_compat_translate(chunk, api_base, api_key, model,
                              glossary_terms=None, prev_entries=None):
    """
    通用 OpenAI 兼容格式翻译（lemon / deepseek 共用）。
    带限流控制、指数退避重试、断点友好的 batch 迭代。
    """
    import requests, time

    out = []
    for batch in chunked(chunk, BATCH_SIZE):
        prompt = _build_translate_prompt(batch, glossary_terms, prev_entries)

        if TRANSLATION_DELAY > 0:
            time.sleep(TRANSLATION_DELAY)

        batch_out = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{api_base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3, "max_tokens": 4000},
                    timeout=60,
                )
                resp.raise_for_status()
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                batch_out = _parse_translate_response(content, batch)
                break
            except Exception as e:
                err = str(e)
                is_rate_limit = "429" in err or "Too Many Requests" in err
                if is_rate_limit and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"WARNING: Rate limited, retrying in {delay}s... ({attempt+1}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(delay)
                    continue
                print(f"WARNING: API batch failed after {attempt+1} attempts: {e}", file=sys.stderr)
                batch_out = [{"idx": s["idx"], "time": s["time"], "text": s["text"]} for s in batch]
                break

        out.extend(batch_out)
    return out


def lemon_translate_chunk(chunk, glossary_terms=None, prev_entries=None):
    """LemonAPI / Moonshot 翻译。"""
    return _openai_compat_translate(
        chunk, LEMON_API_BASE, LEMON_API_KEY, LEMON_MODEL,
        glossary_terms=glossary_terms, prev_entries=prev_entries,
    )


def deepseek_translate_chunk(chunk, glossary_terms=None, prev_entries=None):
    """DeepSeek V3 翻译。"""
    return _openai_compat_translate(
        chunk, DEEPSEEK_API_BASE, DEEPSEEK_API_KEY, DEEPSEEK_MODEL,
        glossary_terms=glossary_terms, prev_entries=prev_entries,
    )


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
            translated = []
            for t in texts:
                try:
                    r = _translate_with_timeout(translator, [t], 10)
                    translated.append(r[0] if r else t)
                except Exception:
                    translated.append(t)
        for src, txt in zip(batch, translated):
            zh = (txt or src["text"]).strip()
            zh = re.sub(r"\s+", " ", zh)
            out.append({"idx": src["idx"], "time": src["time"], "text": zh})
    return out


def _parse_glm_response(response_text, original_chunk):
    lines = [l.strip() for l in response_text.strip().split("\n") if l.strip()]
    translated_texts, i = [], 0
    while i < len(lines):
        if lines[i].isdigit():
            i += 1
            if i < len(lines) and "-->" in lines[i]:
                i += 1
            if i < len(lines) and not lines[i].isdigit():
                translated_texts.append(lines[i]); i += 1
            else:
                translated_texts.append("")
        else:
            i += 1
    result = []
    for i, orig in enumerate(original_chunk):
        zh = translated_texts[i].strip() if i < len(translated_texts) and translated_texts[i].strip() else orig["text"]
        result.append({"idx": orig["idx"], "time": orig["time"], "text": zh})
    return result


# =============================================================================
# AI backends (claude / openclaw)
# =============================================================================

AI_DISABLED = False


def run_ai(prompt, backend_override=None):
    global AI_DISABLED
    if AI_DISABLED:
        raise RuntimeError("AI backend disabled for this run")
    backend = (backend_override or MODEL_BACKEND or "google").lower().strip()
    if backend == "google":
        raise RuntimeError("AI not available when backend=google")
    if backend in ("lemon", "deepseek"):
        api_base = LEMON_API_BASE if backend == "lemon" else DEEPSEEK_API_BASE
        api_key  = LEMON_API_KEY  if backend == "lemon" else DEEPSEEK_API_KEY
        model    = LEMON_MODEL    if backend == "lemon" else DEEPSEEK_MODEL
        import requests
        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 4000},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            if any(x in str(e).lower() for x in ["quota", "credit", "额度"]):
                AI_DISABLED = True
            raise RuntimeError(f"{backend} API failed: {e}")
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
        if proc.returncode != 0:
            msg = out or proc.stderr
            if any(x in msg.lower() for x in ["authenticate", "quota", "403", "credit"]) or "额度" in msg:
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
    # claude CLI
    claude = shutil.which("claude")
    if not claude:
        AI_DISABLED = True
        raise RuntimeError("claude CLI not found")
    proc = subprocess.run(
        [claude, "--permission-mode", "bypassPermissions", "--print", prompt],
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        msg = out or proc.stderr
        if any(x in msg.lower() for x in ["authenticate", "quota", "403", "credit"]) or "额度" in msg:
            AI_DISABLED = True
        raise RuntimeError(f"claude rc={proc.returncode}: {msg[:500]}")
    return out


def ai_translate_chunk(chunk, glossary_terms, prev_entries=None):
    prompt = _build_translate_prompt(chunk, glossary_terms, prev_entries)
    return _parse_translate_response(run_ai(prompt), chunk)


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


# =============================================================================
# Glossary
# =============================================================================

def apply_glossary(text, glossary_terms):
    fixed = text
    for en, zh in glossary_terms:
        if not en or not zh:
            continue
        fixed = re.sub(rf"\b{re.escape(en)}\b", zh, fixed, flags=re.IGNORECASE)
    fixed = fixed.replace("摩托十字", "越野摩托车").replace("越野摩托车越野", "越野摩托车")
    return fixed


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


# =============================================================================
# Meta generation
# =============================================================================

def _normalize_generated_title(title):
    title = (title or "").strip()
    title = re.sub(r"[\s._-]+$", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _fallback_translate_title(raw_title):
    raw = (raw_title or "").strip()
    if not raw:
        return "越野摩托车技术解析"
    phrase_map = [
        ("Standing Position", "站姿技巧"), ("Attack Position", "攻击姿态"),
        ("Body Position", "身体姿态"), ("Corner Control", "弯道控制"),
        ("Jump Technique", "跳跃技术"), ("Start Technique", "起步技术"),
        ("Whoops Technique", "起伏地形技巧"), ("Race Breakdown", "比赛拆解"),
    ]
    word_map = [
        ("Motocross", "越野摩托"), ("Supercross", "室内超级越野"),
        ("Corner", "弯道"), ("Jump", "跳跃"), ("Brake", "刹车"),
        ("Start", "起步"), ("Technique", "技巧"), ("Training", "训练"),
        ("Tips", "技巧"), ("Control", "控制"), ("Position", "姿态"),
    ]
    title = raw
    for en, zh in phrase_map:
        title = re.sub(re.escape(en), zh, title, flags=re.IGNORECASE)
    for en, zh in word_map:
        title = re.sub(rf"\b{re.escape(en)}\b", zh, title, flags=re.IGNORECASE)
    title = re.sub(r"\b(with|and|the|a|an|of|for|to|in|on)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if re.search(r"[A-Za-z]{3,}", title):
        try:
            from deep_translator import GoogleTranslator
            translated = (GoogleTranslator(source="auto", target="zh-CN").translate(raw) or "").strip()
            if translated:
                title = translated
        except Exception:
            pass
    title = re.sub(r"\b[A-Za-z]+\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    title = _normalize_generated_title(title)
    return (title or "越野摩托车技术解析")[:20]


def generate_meta(zh_srt_text, youtube_url, video_title=""):
    if not AI_DISABLED:
        prompt = f"""你是越野摩托车内容运营专家。请根据以下中文字幕内容，为 B 站转载视频生成高质量投稿文案。

【标题要求】20字以内，突出比赛看点/车手/技术点，禁止空泛表达。
【简介要求】开头注明搬运来源，500字左右，有信息密度，自然引导关注。

中文字幕（节选）：
{zh_srt_text[:4000]}
原视频标题：{video_title}

输出 JSON：{{"title":"...","desc":"..."}}
只输出 JSON。
"""
        try:
            text = run_ai(prompt, backend_override=META_BACKEND)
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                obj = json.loads(m.group())
                title = _normalize_generated_title(obj.get("title", ""))
                desc  = obj.get("desc", "")
                if title and len(title) > 3 and not re.search(r"[A-Za-z]{4,}", title):
                    return {"title": title[:20], "desc": desc}
        except Exception as e:
            if (META_BACKEND or "google").lower().strip() != "google":
                print(f"WARNING: AI meta failed: {e}", file=sys.stderr)

    title = _fallback_translate_title(video_title)
    desc  = (
        f"本视频搬运自 YouTube 原博主，版权归原作者所有。\n\n"
        f"原视频：{youtube_url}\n\n"
        f"【内容简介】\n{title}相关技术分析视频，适合越野摩托车爱好者观看学习。\n\n"
        f"喜欢的朋友请点赞收藏，关注持续更新更多越野摩托车内容！"
    )
    return {"title": title, "desc": desc}


# =============================================================================
# SRT 写出
# =============================================================================

def entries_to_srt(entries):
    lines = []
    for i, e in enumerate(entries, 1):
        text = post_process_subtitle(e["text"])
        if not text:
            continue
        lines.append(f"{i}\n{e['time']}\n{text}\n")
    return "\n".join(lines)


# =============================================================================
# 主翻译流程（带断点续传）
# =============================================================================

def translate_entries(entries, video_dir, glossary_terms, backend):
    """翻译全部字幕条目，支持断点续传缓存。"""
    cache = load_cache(video_dir)
    results = []
    untranslated = []

    # 先从缓存恢复
    for e in entries:
        key = f"{e['idx']}|{e['text']}"
        if key in cache:
            results.append({"idx": e["idx"], "time": e["time"], "text": cache[key]})
        else:
            untranslated.append(e)

    if not untranslated:
        print(f"[cache] All {len(entries)} entries restored from cache.", file=sys.stderr)
        return results

    print(f"[translate] {len(untranslated)} entries to translate ({len(results)} from cache).", file=sys.stderr)

    chunks = list(chunked(untranslated, BATCH_SIZE))
    translated = []

    for i, chunk in enumerate(chunks):
        prev = results[-3:] if results else None
        glossary_json = json.dumps(glossary_terms, ensure_ascii=False)

        try:
            if backend == "lemon":
                out = lemon_translate_chunk(chunk, glossary_json, prev)
            elif backend == "deepseek":
                out = deepseek_translate_chunk(chunk, glossary_json, prev)
            elif backend == "google":
                out = google_translate_chunk(chunk)
            else:
                out = ai_translate_chunk(chunk, glossary_json, prev)
        except Exception as ex:
            print(f"WARNING: chunk {i} failed: {ex}", file=sys.stderr)
            out = [{"idx": e["idx"], "time": e["time"], "text": e["text"]} for e in chunk]

        translated.extend(out)
        results.extend(out)

        # 存缓存（每 batch 一次）
        for src, res in zip(chunk, out):
            cache[f"{src['idx']}|{src['text']}"] = res["text"]
        save_cache(video_dir, cache)
        print(f"[translate] batch {i+1}/{len(chunks)} done", file=sys.stderr)

    # 按原始顺序重排
    idx_map = {f"{e['idx']}|{e['text']}": r for e, r in zip(untranslated, translated)}
    final = []
    for e in entries:
        key = f"{e['idx']}|{e['text']}"
        if key in cache:
            final.append({"idx": e["idx"], "time": e["time"], "text": cache[key]})
        else:
            final.append(e)
    return final


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Auto-translate subtitles to Chinese for Bilibili.")
    parser.add_argument("video_dir", help="Directory containing the video and subtitle files")
    parser.add_argument("youtube_url", nargs="?", default="", help="Original YouTube URL")
    parser.add_argument("--source-srt", dest="source_srt", default=None,
                        help="Explicit source SRT file to translate (skips auto-detection)")
    args = parser.parse_args()

    video_dir   = args.video_dir
    youtube_url = args.youtube_url
    backend     = MODEL_BACKEND

    # 确定源字幕文件
    if args.source_srt and os.path.exists(args.source_srt):
        srt_path = args.source_srt
        print(f"Using specified source SRT: {srt_path}")
    else:
        srt_path = read_subtitle(video_dir)
    if not srt_path:
        print("ERROR: No subtitle file found.", file=sys.stderr)
        sys.exit(1)
    print(f"Source subtitle: {srt_path}")

    entries = load_entries(srt_path)
    print(f"Loaded {len(entries)} subtitle entries.")

    # 加载词汇表
    glossary = load_glossary()

    # 可选：从样本提取新术语
    if not AI_DISABLED and len(entries) > 10:
        sample = " ".join(e["text"] for e in entries[:30])
        try:
            new_terms = extract_glossary(sample)
            glossary = merge_glossary(glossary, new_terms.get("terms", []))
            save_glossary(glossary)
        except Exception:
            pass

    glossary_terms = glossary.get("terms", [])

    # 翻译（带断点续传）
    translated = translate_entries(entries, video_dir, glossary_terms, backend)

    # 应用词汇表后处理
    for e in translated:
        e["text"] = apply_glossary(e["text"], glossary_terms)
        e["text"] = re.sub(POST_NOISE_RE, "", e["text"]).strip()

    # 写出 zh_final.srt
    zh_srt_path = os.path.join(video_dir, "zh_final.srt")
    with open(zh_srt_path, "w", encoding="utf-8") as f:
        f.write(entries_to_srt(translated))
    print(f"Written: {zh_srt_path}")

    # 生成 meta.json
    zh_text = " ".join(e["text"] for e in translated[:80])
    video_title = ""
    try:
        info_files = glob.glob(os.path.join(video_dir, "*.info.json"))
        if info_files:
            info = json.load(open(info_files[0], encoding="utf-8"))
            video_title = info.get("title", "")
    except Exception:
        pass

    meta = generate_meta(zh_text, youtube_url, video_title)
    meta_path = os.path.join(video_dir, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"Written: {meta_path}")
    print(f"Title: {meta['title']}")


if __name__ == "__main__":
    main()
