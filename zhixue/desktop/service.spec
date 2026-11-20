from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


root = Path(SPECPATH).resolve().parent
web = root / "dist"
migrations = root / "migrations"
assets = [path for path in web.rglob("*") if path.is_file()]
assets += [migrations / "env.py", *sorted((migrations / "versions").glob("*.py"))]
assets += sorted((root / "backend" / "tessdata").glob("*"))
voice_models = root / "backend" / "voice_models"
assets += [voice_models / name for name in ("ggml-small-q5_1.bin", "LICENSE", "README.md")]
data = [(str(path), path.relative_to(root).parent.as_posix()) for path in assets]

analysis = Analysis(
    [str(root / "desktop" / "service.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("pywhispercpp"),
    datas=data + collect_data_files("tzdata") + copy_metadata("pywhispercpp"),
    hiddenimports=[
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
    ],
)
bundle = PYZ(analysis.pure)
executable = EXE(
    bundle, analysis.scripts, analysis.binaries, analysis.datas, [],
    name="zhixue-service",
    console=True,
    upx=False,
)
