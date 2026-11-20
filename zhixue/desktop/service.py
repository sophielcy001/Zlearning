from multiprocessing import freeze_support
import sys


if __name__ == "__main__":
    freeze_support()
    if sys.argv[1:2] == ["--worker"]:
        from backend.worker import main

        main(sys.argv[2:])
    elif sys.argv[1:2] == ["--parse-document"]:
        from backend.documents import main

        main(sys.argv[2:])
    elif sys.argv[1:2] == ["--transcribe-audio"]:
        from backend.voice import main

        main(sys.argv[2:])
    else:
        from backend.launch import main

        raise SystemExit(main())
