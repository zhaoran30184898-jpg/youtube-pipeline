---
name: dirtbike-pipeline
description: 越野摩托车视频搬运流水线（ABC优化版）。当用户说"搬运视频"、"处理视频"、"制作字幕并投稿"、"dirtbike pipeline"时使用。输入 YouTube URL，自动完成下载、ASR纠错、翻译、质量验证、封面、字幕烧录、投稿全流程，无需人工确认。
---

# Dirtbike Pipeline Skill (ABC Optimized)

## 触发条件

用户说以下任意内容时启动：
- 搬运视频 / 处理视频 / 帮我搬运
- 制作字幕并投稿
- dirtbike pipeline
- 越野视频流水线

## 输入

YouTube 视频 URL（必须）

## 一键全自动流程（ABC优化版）

```bash
bash ~/.openclaw/workspace/skills/dirtbike-pipeline/pipeline.sh <YouTube_URL>
```

脚本自动执行全部 6 步，无需人工确认：

| 步骤 | 内容 | 优化 |
|------|------|------|
| 1/6 | yt-dlp 下载视频 | - |
| 2/6 | **Whisper 转录** (优化参数) → en.srt | **B: VAD过滤+领域提示** |
| 3/6 | **ASR 纠错** → en_corrected.srt | **A: 谐音错误修正** |
| 4/6 | 翻译字幕 → zh_final.srt，生成标题/简介 | - |
| 5/6 | **质量验证** → quality_report.json | **C: 长度/术语检查** |
| 6/6 | 生成封面 + 硬烧字幕 → 投稿 | - |

## ABC 优化详解

### A. ASR 纠错层 (asr_corrector.py)

在翻译前修正语音识别错误：

```bash
# 示例纠错
"home shop" → "holeshot"
"burns" → "berm"
"whole shot" → "holeshot"
"KT them" → "KTM"
```

- 基于谐音/近音错误模式
- 支持上下文感知修正
- 自动从 glossary.json 生成纠错模式

### B. Whisper 高级参数 (whisper_config.py)

优化转录质量：

```python
# 关键参数
vad_filter=True                    # 过滤静音段
condition_on_previous_text=True    # 上下文连贯
word_timestamps=True              # 词级时间戳
initial_prompt="motocross terms..." # 领域提示
temperature=0.0                   # 确定性输出
```

### C. 质量验证器 (quality_validator.py)

翻译后自动检查：

- **长度比例检查**：中文长度应在英文的 25%-200% 之间
- **术语一致性**：确保 glossary 术语被正确使用
- **重复检测**：识别相似度>85%的连续字幕
- **格式检查**：SRT格式正确性

生成 `quality_report.json` 包含质量评分和改进建议。

### D. 翻译速率限制 (auto_translate.py)

为防止 LemonAPI (Moonshot) 返回 429 "Too Many Requests" 错误，已内置智能速率控制：

- **批次延迟**：每次 API 请求前等待 `DIRTBIKE_TRANSLATION_DELAY` 秒（默认 2s）
- **批次大小**：每批处理 `DIRTBIKE_BATCH_SIZE` 条字幕（默认 50）
- **指数退避重试**：遇到 429 错误时自动重试，延迟呈指数增长（2s → 4s → 8s）
- **智能错误处理**：记录重试次数，超过 `DIRTBIKE_MAX_RETRIES` 后抛出异常

如仍遇到 429 错误，可增加延迟：

```bash
export DIRTBIKE_TRANSLATION_DELAY=3.0  # 增加到3秒
export DIRTBIKE_BATCH_SIZE=40          # 减少每批数量
bash ~/.openclaw/workspace/skills/dirtbike-pipeline/pipeline.sh <URL>
```

## 环境变量控制

可通过环境变量开关优化功能：

```bash
# 关闭 ASR 纠错
export DIRTBIKE_ASR_CORRECTION=0

# 关闭质量验证
export DIRTBIKE_QUALITY_CHECK=0

# 使用基础 Whisper 参数
export DIRTBIKE_WHISPER_ADVANCED=0

# 调整翻译速率限制（防止429错误）
export DIRTBIKE_TRANSLATION_DELAY=2.0      # 每次API请求间隔（秒）
export DIRTBIKE_BATCH_SIZE=50              # 每批翻译字幕数量
export DIRTBIKE_MAX_RETRIES=3              # 429错误重试次数
export DIRTBIKE_RETRY_BASE_DELAY=2.0       # 重试基础延迟（秒）

# 运行 pipeline
bash ~/.openclaw/workspace/skills/dirtbike-pipeline/pipeline.sh <URL>
```

## 字幕翻译规则（auto_translate.py）[已优化]

- **噪音过滤**：自动跳过 `[Music]`、`【音乐】`、`[Applause]`、`(laughter)`、`♪...♪`、纯语气词（uh/um/yeah 等）
- **去重**：连续相同字幕行自动合并
- **断句优化**：gap_threshold 3.0s → 2.0s，减少单条过长；在连词（and/but/so/because）处主动拆分
- **长度限制**：每条字幕不超过 35 中文字符
- **后处理**：自动去除冗余空格、优化标点、在合适位置断句
- **术语表**：每次翻译后提取术语，与 `glossary.json` 合并去重并持久化，后续翻译优先使用
- **专业术语**：内置 60+ 越野摩托车专业术语翻译（KTM、Supercross、holeshot、berm、whoops 等）
- **风格**：口语化、精炼、有速度感

## 元数据规则（meta.json）[已优化]

- **标题**：从视频内容提炼，突出比赛看点/车手/组别/赛道/技术点；≤20字，禁止空泛；示例："KTM战车入弯攻略"、"270cc公开组起步分析"
- **简介**：口语化、有信息密度；开头注明"搬运自 YouTube 原博主，版权归原作者所有"；交代视频主题、适合谁看、关键技术点；目标 500 字

## 封面规则（cover_gen.py）[已优化]

- 背景：视频 60% 时间点帧，增强对比度/饱和度/锐度
- 文字：居中放入 16:9 安全区（左右各 200px margin）
- 视觉优化：
  - 速度条更宽更长（560px → 760px），更有速度感
  - 底部渐变更强，确保文字区域清晰
  - 主标题更大（90px → 100px）+ 阴影效果
  - 强调色更醒目

## 持久术语表

- 路径：`~/.openclaw/workspace/skills/dirtbike-pipeline/glossary.json`
- 格式：`{"terms": [["英文", "中文"], ...]}`
- 每次运行自动合并新术语，跨视频积累

## 文件结构

```
~/.openclaw/workspace/skills/dirtbike-pipeline/
├── SKILL.md              # 本文件
├── pipeline.sh           # 主流水线（全自动6步，ABC优化）
├── whisper_transcribe.py # Whisper转录（B优化）
├── whisper_config.py     # Whisper参数配置中心
├── asr_corrector.py      # ASR纠错层（A优化）
├── auto_translate.py     # 字幕翻译 + 内容生成
├── quality_validator.py  # 质量验证器（C优化）
├── cover_gen.py          # Pillow封面生成（备选）
├── cover_html.py         # HTML/CSS封面生成
├── upload.sh             # B站投稿封装
└── glossary.json         # 持久术语表
```

## 输出文件

```
~/Downloads/subtitle-archive/<id>-<title>/
├── <title>.mp4           # 原始视频
├── audio.wav             # 提取的音频
├── en.srt                # 原始英文字幕
├── en_corrected.srt      # ASR纠错后字幕（A优化产出）
├── zh_final.srt          # 中文字幕
├── quality_report.json   # 质量验证报告（C优化产出）
├── meta.json             # 标题 + 简介
├── cover_new.jpg         # 封面
└── <title>_subbed.mp4    # 烧录字幕后的投稿视频
```

## 注意事项

- biliup 必须在 `~/.openclaw/workspace/scripts/` 目录下运行（cookies.json 在此）
- 投稿类型为转载（copyright=2），来源填 YouTube 原链接
- 字幕字体优先 PingFang SC，备选 STHeiti
- 字幕烧录固定使用 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`
- 需要：`claude` CLI、`ffmpeg-full`、`yt-dlp`、`biliup`、`Pillow`
