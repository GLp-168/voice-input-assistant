"""PyInstaller entry point for the Windows runtime health checker."""

from voice_input.healthcheck import main


if __name__ == "__main__":
    raise SystemExit(main())
