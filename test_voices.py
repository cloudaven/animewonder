"""Generate one mp3 sample per voice in the export pool so Justen can
preview them before running an export. Output goes to a folder on the
Desktop. No downloads — Edge Neural streams from Microsoft's TTS service."""
import asyncio
import os
import sys

import edge_tts


SAMPLE_TEXT = (
    "I am the Shadow Monarch. From the deepest dungeon I have returned. "
    "The arch demons trembled before my throne. Now I stand at the gate, "
    "and the world will know my name."
)

VOICES = [
    ("01_male_narrator_Guy",         "en-US-GuyNeural"),
    ("02_male_authority_Christopher","en-US-ChristopherNeural"),
    ("03_male_modern_Andrew",        "en-US-AndrewNeural"),
    ("04_female_lead_Jenny",         "en-US-JennyNeural"),
    ("05_female_soft_Aria",          "en-US-AriaNeural"),
    ("06_female_modern_Emma",        "en-US-EmmaNeural"),
]


async def render(text: str, voice: str, out: str) -> int:
    comm = edge_tts.Communicate(text, voice)
    chunks = []
    async for c in comm.stream():
        if c["type"] == "audio":
            chunks.append(c["data"])
    data = b"".join(chunks)
    with open(out, "wb") as fh:
        fh.write(data)
    return len(data)


async def main():
    out_dir = os.path.join(os.path.expanduser("~"), "Desktop", "AnimeWonder_Voice_Samples")
    os.makedirs(out_dir, exist_ok=True)
    print(f"output: {out_dir}\n")
    for label, voice in VOICES:
        out = os.path.join(out_dir, f"{label}.mp3")
        try:
            size = await render(SAMPLE_TEXT, voice, out)
            print(f"  {label:<32} -> {size/1024:6.1f} KB  ({voice})")
        except Exception as e:
            print(f"  {label:<32} FAILED: {e}")
    print(f"\nOpen {out_dir} and play each mp3. Pick the ones you like.")


if __name__ == "__main__":
    asyncio.run(main())
