from hashlib import file_digest, sha256
from pathlib import Path
import ssl
from threading import Event, Lock
from urllib.request import HTTPSHandler, HTTPRedirectHandler, build_opener

import certifi


DIMENSIONS = 384
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_URL = f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{MODEL_REVISION}/"
ASSETS = (
    ("tokenizer.json", 17_082_730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
    ("onnx/model_quantized.onnx", 118_308_185, "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
)
MODEL_BYTES = sum(size for _, size, _ in ASSETS)
CHUNK_TOKENS = 192
OVERLAP = 24
INFERENCE_BATCH = 16


class HTTPSOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise OSError("模型下载需要 HTTPS。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verified_asset(path: Path, size: int, digest: str) -> bool:
    try:
        if path.stat().st_size != size:
            return False
        with path.open("rb") as source:
            return file_digest(source, "sha256").hexdigest() == digest
    except FileNotFoundError:
        return False


def install_model(folder: Path, stopped: Event) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    opener = build_opener(HTTPSOnly(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    for name, expected_size, expected_digest in ASSETS:
        if stopped.is_set():
            return
        path = folder / Path(name).name
        if verified_asset(path, expected_size, expected_digest):
            continue
        temporary = path.with_suffix(".part")
        digest = sha256()
        size = 0
        try:
            # Requests identify public weights and the pinned revision only.
            url = MODEL_URL + name + "?download=true"
            with opener.open(url, timeout=30) as source, temporary.open("wb") as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    if stopped.is_set():
                        return
                    size += len(block)
                    if size > expected_size:
                        raise ValueError("模型文件超过预期大小。")
                    digest.update(block)
                    target.write(block)
            if size != expected_size or digest.hexdigest() != expected_digest:
                raise ValueError("模型文件未通过校验。")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class LocalEmbeddings:
    def __init__(self, folder: Path):
        for name, size, digest in ASSETS:
            if not verified_asset(folder / Path(name).name, size, digest):
                raise ValueError("本地语义模型未通过校验。")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        ort.disable_telemetry_events()
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(folder / "model_quantized.onnx"), sess_options=options, providers=["CPUExecutionProvider"],
        )
        self.inputs = {entry.name for entry in self.session.get_inputs()}
        self.lock = Lock()

    def encode(self, texts: list[str]):
        import numpy as np

        encodings = self.tokenizer.encode_batch(texts)
        mask = np.asarray([entry.attention_mask for entry in encodings], dtype=np.int64)
        inputs = {"input_ids": np.asarray([entry.ids for entry in encodings], dtype=np.int64), "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            inputs["token_type_ids"] = np.zeros_like(mask)
        hidden, = self.session.run(["last_hidden_state"], inputs)
        averaged = (hidden * mask[..., None].astype(np.float32)).sum(axis=1) / mask.sum(axis=1, keepdims=True)
        norms = np.linalg.norm(averaged, axis=1, keepdims=True)
        if averaged.shape[1] != DIMENSIONS or not np.isfinite(norms).all() or np.any(norms == 0):
            raise ValueError("语义模型返回了无效向量。")
        return (averaged / norms).astype("<f4")

    def query(self, query: str):
        with self.lock:
            return self.encode(["query: " + query])[0]

    def passages(self, texts: list[str], stopped: Event):
        chunks = []
        results = [[] for _ in texts]
        for index, body in enumerate(texts):
            offsets = self.tokenizer.encode(body, add_special_tokens=False).offsets
            for start in range(0, len(offsets), CHUNK_TOKENS - OVERLAP):
                end = min(start + CHUNK_TOKENS, len(offsets))
                left, right = offsets[start][0], offsets[end - 1][1]
                if right > left:
                    chunks.append((index, left, right, body[left:right]))
                if end == len(offsets):
                    break
        # Similar lengths limit padding when embedding mixed material.
        chunks.sort(key=lambda item: len(item[3]))
        for offset in range(0, len(chunks), INFERENCE_BATCH):
            if stopped.is_set():
                return results
            batch = chunks[offset:offset + INFERENCE_BATCH]
            vectors = self.encode(["passage: " + body for _, _, _, body in batch])
            for (index, left, right, _), vector in zip(batch, vectors, strict=True):
                results[index].append((left, right, vector))
        return results
