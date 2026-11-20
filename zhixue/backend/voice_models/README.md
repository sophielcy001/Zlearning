# 本机 Whisper 语音模型

智学随桌面安装包提供 Whisper small 多语言模型的 Q5_1 量化版。上游权重采用 MIT 许可，见同目录的 LICENSE。

源码目录运行 `uv run --locked python -m backend.voice_model` 可准备模型。打包入口会校验已有模型，并在需要时从固定版本的上游仓库下载。应用内录音和识别流程只读取本地模型。

| 项目 | 值 |
| --- | --- |
| 模型文件 | ggml-small-q5_1.bin |
| 字节数 | 190085487 |
| SHA-256 | ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb |

[上游 Whisper 权重许可](https://github.com/openai/whisper#license) · [whisper.cpp 模型版本](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1)
