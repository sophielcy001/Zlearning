from subprocess import SubprocessError, TimeoutExpired
from threading import Lock
from typing import Literal
from uuid import UUID
import wave

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .recognition import Recognition
from .voice import MAX_SECONDS, MAX_WAV_BYTES, read_pcm, recognizer_available, transcribe_local


async def recording_pcm(request: Request) -> bytes:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "audio/wav":
        raise HTTPException(415, "请使用应用的录音入口。")
    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_WAV_BYTES:
            raise HTTPException(413, f"录音不能超过 {MAX_SECONDS} 秒。")
        audio.extend(chunk)
    try:
        return read_pcm(audio)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except (wave.Error, EOFError) as error:
        raise HTTPException(422, "录音格式无法读取，请重新录音。") from error


def voice_router(recognition: Recognition) -> APIRouter:
    router = APIRouter(prefix="/api/voice")
    recognizing = Lock()

    @router.get("")
    def voice_status():
        return {"available": recognizer_available(), "maxSeconds": MAX_SECONDS}

    @router.post("/transcribe")
    async def transcribe(request: Request, language: Literal["zh", "en"] = "zh"):
        samples = await recording_pcm(request)
        if not recognizer_available():
            raise HTTPException(503, "本机语音模型暂不可用。你可以继续键入。")
        if not recognizing.acquire(blocking=False):
            raise HTTPException(429, "本机正在识别另一段录音，请稍后重试。")
        try:
            text = await run_in_threadpool(transcribe_local, samples, language)
        except TimeoutExpired as error:
            raise HTTPException(422, "本机识别超时，可以缩短录音后重试。") from error
        except (OSError, SubprocessError, ValueError) as error:
            raise HTTPException(503, "本机语音识别暂时失败。请重试或继续键入。") from error
        finally:
            recognizing.release()
        if not text:
            raise HTTPException(422, "未识别到清晰的语音，请靠近麦克风重试或继续键入。")
        return {"text": text}

    @router.post("/external")
    async def recognize_external(request: Request, authorizationId: UUID, language: Literal["zh", "en"] = "zh"):
        samples = await recording_pcm(request)
        return {"text": await run_in_threadpool(recognition.voice, str(authorizationId), samples, language)}

    return router
