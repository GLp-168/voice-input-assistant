"""Configuration defaults for the voice input package."""

DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_MAX_RECORDING_SECONDS = 2 * 60 * 60
DEFAULT_MAX_IMPORT_AUDIO_SECONDS = DEFAULT_MAX_RECORDING_SECONDS
DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS = 60.0
DIRECT_AUDIO_FILE_EXTENSIONS = (".wav", ".flac", ".ogg")
TRANSCODABLE_AUDIO_FILE_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wma",
    ".opus",
    ".webm",
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".mpeg",
    ".mpg",
    ".m4v",
    ".amr",
    ".3gp",
    ".aiff",
    ".aif",
    ".caf",
)
SUPPORTED_AUDIO_FILE_EXTENSIONS = DIRECT_AUDIO_FILE_EXTENSIONS + TRANSCODABLE_AUDIO_FILE_EXTENSIONS
DEFAULT_DUMMY_TEXT = "[ASR dummy] audio received"
