"""Checked by mypy (`mypy tests/typing_check.py`), never run: the types must catch these mistakes.

Each `# type: ignore[...]` marks a line that has to be an error. Under strict mypy an ignore
that is not needed fails the check, so a type that stops catching the mistake breaks CI.
"""

import sume


def calls_that_type_check() -> None:
    sume.run("gpt-image-2", "a kite", quality="low", num_images=2, aspect_ratio="16:9")
    sume.run("seedance-2-mini", "a boat", duration=4, resolution="480p", image="photo.png")
    sume.run("veed-subtitles", video="clip.mp4", duration=12, captions={"style": "glass", "position": "bottom"})
    sume.run("some-new-model", "x", input={"field_from_next_month": 1})
    model: sume.ModelId = "gpt-image-2"
    sume.estimate(model, "a kite")


def mistakes_the_types_catch() -> None:
    sume.run("gpt-image-2", "a kite", qualty="low")  # type: ignore[call-arg]
    sume.run("gpt-image-2", "a kite", aspect_ratio="7:3")  # type: ignore[arg-type]
    sume.run("gpt-image-2", "a kite", num_images="2")  # type: ignore[arg-type]
    sume.run("veed-subtitles", captions={"position": "middle"})  # type: ignore[typeddict-item]
    model: sume.ModelId = "gpt-image-3"  # type: ignore[assignment]
    del model
