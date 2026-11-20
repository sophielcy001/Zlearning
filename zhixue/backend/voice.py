from importlib.util import find_spec
from io import BytesIO
import json
from os import cpu_count
from pathlib import Path
import subprocess
import sys
import wave


SAMPLE_RATE = 16_000
MAX_SECONDS = 60
MAX_PCM_BYTES = SAMPLE_RATE * MAX_SECONDS * 2
MAX_WAV_BYTES = MAX_PCM_BYTES + 4096
MODEL = Path(__file__).resolve().with_name("voice_models") / "ggml-small-q5_1.bin"
MODEL_SIZE = 190_085_487


def recognizer_available() -> bool:
    try:
        return MODEL.stat().st_size == MODEL_SIZE and find_spec("_pywhispercpp") is not None
    except OSError:
        return False


def read_pcm(recording: bytes) -> bytes:
    with wave.open(BytesIO(recording), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("录音格式无法读取，请重新录音。")
        count = audio.getnframes()
        if not SAMPLE_RATE // 4 <= count <= SAMPLE_RATE * MAX_SECONDS:
            raise ValueError(f"请录制 0.25 至 {MAX_SECONDS} 秒的语音。")
        samples = audio.readframes(count)
        if len(samples) != count * 2:
            raise ValueError("录音数据不完整，请重新录音。")
        return samples


def transcribe_local(samples: bytes, language: str) -> str:
    entry = ["--transcribe-audio"] if getattr(sys, "frozen", False) else ["-m", "backend.voice"]
    result = subprocess.run(
        [sys.executable, *entry, language], cwd=Path(__file__).resolve().parents[1], input=samples,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    result_text = json.loads(result.stdout)
    if not isinstance(result_text, str):
        raise ValueError("Voice parser returned an unexpected result.")
    return result_text


def recognize(samples: bytes, language: str) -> str:
    import numpy as np
    from pywhispercpp.model import Model

    # Only an existing, explicitly sized model path is accepted by the local worker.
    if not recognizer_available():
        raise FileNotFoundError("The bundled voice model is missing or incomplete.")
    audio = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768
    if not audio.size or np.max(np.abs(audio)) < 0.006:
        return ""
    model = Model(
        str(MODEL), n_threads=min(4, cpu_count() or 1), context_params={"use_gpu": False},
        params_sampling_strategy=1, beam_search={"beam_size": 5, "patience": 1.0},
        print_progress=False, print_realtime=False, print_timestamps=False,
    )
    segments = model.transcribe(
        audio, language=language, no_context=True, temperature=0.0, temperature_inc=0.0,
        initial_prompt="使用简体中文，保留技术名词。" if language == "zh" else "",
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def main(arguments: list[str]) -> None:
    language, = arguments
    samples = sys.stdin.buffer.read(MAX_PCM_BYTES + 1)
    if language not in {"zh", "en"} or len(samples) > MAX_PCM_BYTES or len(samples) % 2:
        raise SystemExit(1)
    # Native decoding and its model memory are scoped to this subprocess.
    sys.stdout.buffer.write(json.dumps(recognize(samples, language), ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv[1:])
