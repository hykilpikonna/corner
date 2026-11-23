from __future__ import annotations

from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageOps
from scipy.fft import dctn

pillow_heif.register_heif_opener()


def compute_image_fingerprint(image_path: Path) -> tuple[str, bytes, int, int]:
    """Compute perceptual 64-bit DCT pHash, 16x16x3 RGB signature, and (width, height).

    Returns:
        (phash_hex, rgb_sig_bytes, width, height)
        - phash_hex: 16-char lowercase hex string representing 64-bit perceptual hash.
        - rgb_sig_bytes: 768-byte raw uint8 RGB vector (16x16x3) for Workflow B matching.
        - width, height: oriented image dimensions in pixels.
    """
    with Image.open(image_path) as raw_img:
        img = ImageOps.exif_transpose(raw_img)
        width, height = img.size

        rgb = img.convert("RGB")
        # 16x16x3 RGB signature
        rgb_small = rgb.resize((16, 16), Image.Resampling.LANCZOS)
        rgb_sig = rgb_small.tobytes()

        # 64-bit DCT pHash on 32x32 grayscale
        gray = rgb.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
        arr = np.asarray(gray, dtype=np.float64)
        dct_coeffs = dctn(arr, type=2, norm="ortho")
        low_freq = dct_coeffs[:8, :8].flatten()

        # Use median of the 63 AC coefficients (excluding DC at index 0)
        median = float(np.median(low_freq[1:]))
        bits = 0
        for val in low_freq:
            bits = (bits << 1) | (1 if val > median else 0)
        phash_hex = f"{bits:016x}"

        return phash_hex, rgb_sig, width, height


def phash_hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two 64-bit hex perceptual hashes."""
    return (int(h1, 16) ^ int(h2, 16)).bit_count()


def rgb_signature_distance(sig1: bytes, sig2: bytes) -> float:
    """Compute normalized L2 distance in [0, 1] between two 16x16x3 RGB signatures."""
    a = np.frombuffer(sig1, dtype=np.uint8).astype(np.float32) / 255.0
    b = np.frombuffer(sig2, dtype=np.uint8).astype(np.float32) / 255.0
    if a.shape != b.shape or a.size == 0:
        return 1.0
    return float(np.sqrt(np.mean((a - b) ** 2)))
