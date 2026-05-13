"""
Free local animation worker — runs under Python 3.12 with PyTorch + CUDA.
Called as a subprocess from app.py.

Usage:
  py -3.12 animate_worker.py <img_path> <out_path> <seed>

Exits 0 and writes MP4 to out_path on success.
Exits 1 on any error.
"""
import sys, os

def run(img_path, out_path, seed):
    import torch
    from diffusers import StableVideoDiffusionPipeline
    from diffusers.utils import export_to_video
    from PIL import Image

    if not torch.cuda.is_available():
        print("CUDA not available", file=sys.stderr)
        return False

    print("Loading SVD model (downloads once ~7GB on first run)…", flush=True)

    pipe = StableVideoDiffusionPipeline.from_pretrained(
        "stabilityai/stable-video-diffusion-img2vid-xt",
        torch_dtype=torch.float16,
        variant="fp16",
    )
    pipe.enable_model_cpu_offload()
    pipe.unet.enable_forward_chunking()

    print("Generating animation…", flush=True)

    img = Image.open(img_path).convert("RGB").resize((1024, 576))
    generator = torch.manual_seed(int(seed))

    frames = pipe(
        img,
        decode_chunk_size=8,
        generator=generator,
        motion_bucket_id=127,   # higher = more motion
        noise_aug_strength=0.02,
        num_frames=25,
    ).frames[0]

    export_to_video(frames, out_path, fps=6)
    print(f"Saved: {out_path}", flush=True)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: animate_worker.py <img_path> <out_path> <seed>", file=sys.stderr)
        sys.exit(1)

    success = run(sys.argv[1], sys.argv[2], sys.argv[3])
    sys.exit(0 if success else 1)
