from voice_input.controller import VoiceInputController
from voice_input.status import RunState


class FakeRecorder:
    def record_once(self):
        return b"audio"


class FakeASR:
    def transcribe(self, audio):
        assert audio == b"audio"
        return {"text": "你好"}


class FakeInjector:
    def __init__(self):
        self.text = ""

    def inject(self, text):
        self.text = text


def test_controller_runs_one_cycle():
    seen = []
    injector = FakeInjector()
    controller = VoiceInputController(
        recorder=FakeRecorder(),
        asr_engine=FakeASR(),
        injector=injector,
        on_status=seen.append,
    )

    text = controller.run_once()

    assert text == "你好"
    assert injector.text == "你好"
    assert controller.status.state == RunState.IDLE
    assert [status.state for status in seen] == [
        RunState.LISTENING,
        RunState.RECOGNIZING,
        RunState.INJECTING,
        RunState.IDLE,
    ]
