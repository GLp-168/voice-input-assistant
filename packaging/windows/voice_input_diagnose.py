"""PyInstaller entry point for the Windows ASR diagnostic executable."""

from voice_input.diagnose_asr import main


if __name__ == "__main__":
    raise SystemExit(main())
