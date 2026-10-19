from importlib import import_module


def test_voice_input_package_imports() -> None:
    package = import_module("voice_input")

    assert package.__name__ == "voice_input"
