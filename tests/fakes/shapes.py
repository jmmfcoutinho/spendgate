"""Registry: which shape answers which path."""

from tests.fakes.shape_types import Shape
from tests.fakes.shapes_anthropic import Messages
from tests.fakes.shapes_openai import Chat, Embeddings, Responses, Speech, Transcriptions
from tests.fakes.shapes_other import AzureSpeech, BedrockConverse, ElevenLabs, Gemini

SHAPES: tuple[Shape, ...] = (
    Chat(),
    Responses(),
    Embeddings(),
    Transcriptions(),
    Speech(),
    Messages(),
    BedrockConverse(),
    Gemini(),
    AzureSpeech(),
    ElevenLabs(),
)


def shape_for(path: str) -> Shape | None:
    return next((s for s in SHAPES if s.matches(path)), None)
