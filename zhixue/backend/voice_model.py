from hashlib import file_digest, sha256
from tempfile import NamedTemporaryFile

from .voice import MODEL, MODEL_SIZE


MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
    "5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small-q5_1.bin"
)
MODEL_SHA256 = "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"


def matches_model() -> bool:
    if not MODEL.is_file() or MODEL.stat().st_size != MODEL_SIZE:
        return False
    with MODEL.open("rb") as source:
        return file_digest(source, "sha256").hexdigest() == MODEL_SHA256


def prepare_model() -> None:
    if matches_model():
        return

    import requests

    MODEL.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=MODEL.parent, prefix=".download-", delete=False) as target:
        temporary = MODEL.parent / target.name
        try:
            digest = sha256()
            length = 0
            with requests.get(MODEL_URL, stream=True, timeout=(15, 40)) as response:
                response.raise_for_status()
                for block in response.iter_content(1024 * 1024):
                    length += len(block)
                    if length > MODEL_SIZE:
                        raise ValueError("本机语音模型的下载文件大于预期。")
                    digest.update(block)
                    target.write(block)
            if length != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
                raise ValueError("本机语音模型下载不完整或校验不匹配。")
            target.close()
            if not matches_model():
                temporary.replace(MODEL)
        finally:
            target.close()
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    prepare_model()
    print("本机语音模型已就绪。")
