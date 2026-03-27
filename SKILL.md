# Dirtbike Pipeline — SKILL.md

越野摩托车视频自动搬运流水线：YouTube → 翻译中文字幕 → 烧录 → 上传 B 站。

## 快速开始

```bash
# 1. 配置环境变量（首次）
cp .env.example .env
# 编辑 .env，填入 API Key

# 2. 搬运一个视频
bash pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
```

## 流水线步骤

```
[1] 下载视频      yt-dlp 下载最高 1080p，同时获取 VIDEO_ID
[2] 语音转文字    Whisper 生成 en.srt
[3] ASR 纠错      修正专有名词（可选，DIRTBIKE_ASR_CORRECTION=0 跳过）
[4] 翻译字幕      英→中，同步生成 B 站标题 + 简介（meta.json）
[5] 质量验证      对比翻译前后字幕（可选，DIRTBIKE_QUALITY_CHECK=0 跳过）
[6] 封面 + 烧录   生成封面图，ffmpeg 硬烧中文字幕
[7] 上传 B 站     upload.sh 调用 biliup / you-get 上传
```

## 文件结构

```
pipeline.sh           主流水线脚本
auto_translate.py     字幕翻译 + 标题/简介生成
cover_html.py         封面图生成
SKILL.md              本文档
.env.example          环境变量配置示例（复制为 .env 使用）
glossary.json         专有名词术语表（运行时自动维护）

# 运行时生成（skill 目录下）
whisper_transcribe.py Whisper 转录
asr_corrector.py      ASR 纠错
quality_validator.py  翻译质量验证
upload.sh             B 站上传
```

## 翻译后端

| 后端 | 质量 | 价格 | 配置 |
|------|------|------|------|
| `lemon` (默认) | ★★★★ | 中 | DIRTBIKE_LEMON_API_KEY |
| `deepseek` | ★★★★★ | 低 | DEEPSEEK_API_KEY |
| `google` | ★★★ | 免费 | 无需 key |
| `claude` | ★★★★★ | 高 | claude CLI |
| `openclaw` | ★★★★ | 中 | openclaw CLI |

切换后端：

```bash
# 临时切换（单次运行）
DIRTBIKE_MODEL_BACKEND=deepseek bash pipeline.sh "https://..."

# 永久切换（写入 .env）
echo "DIRTBIKE_MODEL_BACKEND=deepseek" >> .env
```

## 断点续传

翻译步骤支持断点续传。每翻译一个 batch 自动写入 `.translate_cache.json`。
网络中断或 API 限流崩溃后，重新运行 pipeline 会自动跳过已翻译的部分。

```bash
# 清除缓存重新翻译
rm ~/Downloads/subtitle-archive/<VIDEO_DIR>/.translate_cache.json
```

## auto_translate.py 直接使用

```bash
# 基本用法（自动检测字幕文件）
python3 auto_translate.py <video_dir> <youtube_url>

# 指定源字幕文件（推荐，与 pipeline.sh 集成时使用）
python3 auto_translate.py <video_dir> <youtube_url> --source-srt <path/to/en.srt>

# 只翻译，不生成 meta（不传 youtube_url）
python3 auto_translate.py <video_dir>
```

## Feature Flags

在 `.env` 里设置，或运行时通过环境变量覆盖：

```bash
DIRTBIKE_ASR_CORRECTION=0    # 跳过 ASR 纠错（步骤3）
DIRTBIKE_QUALITY_CHECK=0     # 跳过质量验证（步骤5）
DIRTBIKE_WHISPER_ADVANCED=0  # 使用 Whisper 基础模式（更快）
```

## 限流参数调优

遇到 429 Too Many Requests 时：

```bash
DIRTBIKE_TRANSLATION_DELAY=5.0   # 加大 batch 间隔（默认 2.0s）
DIRTBIKE_BATCH_SIZE=20           # 减小 batch 大小（默认 50）
DIRTBIKE_MAX_RETRIES=5           # 增加重试次数（默认 3）
```

## 字幕质量说明

- **合并阈值**：间隔 < 1.5s 的碎片字幕会自动合并（避免无关句子被合并）
- **噪音过滤**：自动过滤 [Music]、[Applause]、uh/um 等噪音字幕
- **术语表**：`glossary.json` 自动维护，首次运行会从样本字幕提取专有名词
- **长度限制**：每条中文字幕不超过 35 个字符，超出自动在标点处断开

## 输出文件

```
~/Downloads/subtitle-archive/<VIDEO_ID>-<TITLE>/
├── <TITLE>.mp4              原始视频
├── en.srt                   英文字幕（Whisper）
├── en_corrected.srt         ASR 纠错后字幕
├── zh_final.srt             中文字幕（翻译结果）
├── meta.json                B 站标题 + 简介
├── cover.jpg                封面图
├── <TITLE>_subbed.mp4       烧录字幕后视频（上传用）
├── quality_report.json      翻译质量报告
└── .translate_cache.json    翻译断点缓存（可删除重翻）
```

## 常见问题

**Q: 翻译到一半停了怎么办？**
A: 直接重新运行 `pipeline.sh`，会从缓存断点继续，不重复花钱。

**Q: 术语翻译不准？**
A: 编辑 `glossary.json` 手动添加术语对，格式：`[["英文", "中文"]]`。

**Q: 想换 DeepSeek 但不改 .env？**
A: `DIRTBIKE_MODEL_BACKEND=deepseek bash pipeline.sh "url"`

**Q: 字幕太长显示不下？**
A: 调小 `MAX_CHINESE_CHARS`（auto_translate.py 顶部），或减小 ffmpeg 的 `FontSize`。
