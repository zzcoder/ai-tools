#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path

from huggingface_hub import hf_hub_download


COMFY = Path("/home/zhihongz/ComfyUI")
MODELS = COMFY / "models"
MANIFEST = MODELS / "installed_free_models_manifest.json"


@dataclass(frozen=True)
class ModelFile:
    purpose: str
    repo_id: str
    filename: str
    dest_subdir: str
    dest_name: str | None = None
    required_for_blueprint: str | None = None

    @property
    def dest_path(self) -> Path:
        return MODELS / self.dest_subdir / (self.dest_name or Path(self.filename).name)


MODEL_FILES: list[ModelFile] = [
    # Still image checkpoints.
    ModelFile(
        "SDXL general text-to-image checkpoint",
        "stabilityai/stable-diffusion-xl-base-1.0",
        "sd_xl_base_1.0.safetensors",
        "checkpoints",
    ),
    ModelFile(
        "SDXL Turbo fast text-to-image checkpoint",
        "stabilityai/sdxl-turbo",
        "sd_xl_turbo_1.0_fp16.safetensors",
        "checkpoints",
    ),
    # FLUX.1 Schnell lightweight/fp8 ComfyUI path.
    ModelFile(
        "FLUX.1 Schnell fp8 diffusion model",
        "Comfy-Org/flux1-schnell",
        "flux1-schnell-fp8.safetensors",
        "diffusion_models",
    ),
    ModelFile(
        "FLUX.1 Schnell fp8 checkpoint alias",
        "Comfy-Org/flux1-schnell",
        "flux1-schnell-fp8.safetensors",
        "checkpoints",
    ),
    ModelFile(
        "FLUX CLIP-L text encoder",
        "comfyanonymous/flux_text_encoders",
        "clip_l.safetensors",
        "clip",
    ),
    ModelFile(
        "FLUX T5 XXL fp8 text encoder",
        "comfyanonymous/flux_text_encoders",
        "t5xxl_fp8_e4m3fn.safetensors",
        "clip",
    ),
    ModelFile(
        "FLUX CLIP-L text encoder text_encoders alias",
        "comfyanonymous/flux_text_encoders",
        "clip_l.safetensors",
        "text_encoders",
    ),
    ModelFile(
        "FLUX T5 XXL fp8 text encoder text_encoders alias",
        "comfyanonymous/flux_text_encoders",
        "t5xxl_fp8_e4m3fn.safetensors",
        "text_encoders",
    ),
    ModelFile(
        "FLUX VAE/autoencoder public mirror",
        "MaxedOut/ComfyUI-Starter-Packs",
        "Flux1/vae/ae.safetensors",
        "vae",
        dest_name="ae.safetensors",
    ),
    # Wan 2.2 video blueprint models.
    ModelFile(
        "Wan 2.2 image-to-video high-noise fp8 diffusion model",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        required_for_blueprint="Image to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 image-to-video low-noise fp8 diffusion model",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        required_for_blueprint="Image to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 image-to-video LightX2V high-noise LoRA",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "loras",
        required_for_blueprint="Image to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 image-to-video LightX2V low-noise LoRA",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
        "loras",
        required_for_blueprint="Image to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 text-to-video high-noise fp8 diffusion model",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        required_for_blueprint="Text to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 text-to-video low-noise fp8 diffusion model",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/diffusion_models/wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
        "diffusion_models",
        required_for_blueprint="Text to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 text-to-video LightX2V high-noise LoRA",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
        "loras",
        required_for_blueprint="Text to Video (Wan 2.2)",
    ),
    ModelFile(
        "Wan 2.2 text-to-video LightX2V low-noise LoRA",
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors",
        "loras",
        required_for_blueprint="Text to Video (Wan 2.2)",
    ),
    # Smaller image-to-video fallback.
    ModelFile(
        "Stable Video Diffusion XT image-to-video checkpoint",
        "stabilityai/stable-video-diffusion-img2vid-xt",
        "svd_xt.safetensors",
        "checkpoints",
    ),
    ModelFile(
        "Stable Video Diffusion XT image decoder checkpoint",
        "stabilityai/stable-video-diffusion-img2vid-xt",
        "svd_xt_image_decoder.safetensors",
        "checkpoints",
    ),
    # Lightweight LTX fallback; not tied to the bundled LTX 2.0 19B blueprints.
    ModelFile(
        "LTX Video 2B distilled fp8 lightweight video model",
        "Lightricks/LTX-Video",
        "ltxv-2b-0.9.8-distilled-fp8.safetensors",
        "diffusion_models",
    ),
]


def install_one(item: ModelFile) -> dict:
    item.dest_path.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(item)
    record["dest_path"] = str(item.dest_path)
    if item.dest_path.exists() and item.dest_path.stat().st_size > 0:
        record["status"] = "already_present"
        record["size_bytes"] = item.dest_path.stat().st_size
        return record

    print(f"\n==> {item.purpose}")
    print(f"    {item.repo_id}:{item.filename}")
    print(f"    -> {item.dest_path}")
    try:
        cached = Path(
            hf_hub_download(
                repo_id=item.repo_id,
                filename=item.filename,
                repo_type="model",
                local_files_only=False,
            )
        ).resolve()
        if item.dest_path.exists() or item.dest_path.is_symlink():
            item.dest_path.unlink()
        try:
            os.link(cached, item.dest_path)
            record["link_type"] = "hardlink"
        except OSError:
            try:
                item.dest_path.symlink_to(cached)
                record["link_type"] = "symlink"
            except OSError:
                shutil.copy2(cached, item.dest_path)
                record["link_type"] = "copy"
        record["status"] = "installed"
        record["cache_path"] = str(cached)
        record["size_bytes"] = item.dest_path.stat().st_size
    except Exception as exc:  # Keep installing the rest.
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"FAILED: {record['error']}")
    return record


def main() -> None:
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    records = []
    for item in MODEL_FILES:
        records.append(install_one(item))
        MANIFEST.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    installed = [r for r in records if r["status"] in {"installed", "already_present"}]
    failed = [r for r in records if r["status"] == "failed"]
    print(f"\nInstalled/present: {len(installed)}")
    print(f"Failed: {len(failed)}")
    print(f"Manifest: {MANIFEST}")
    if failed:
        print("\nFailures:")
        for r in failed:
            print(f"- {r['repo_id']}:{r['filename']} -> {r['error']}")


if __name__ == "__main__":
    main()
