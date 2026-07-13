from __future__ import annotations

import zlib


def method_seed(base_seed: int, method: str) -> int:
    """Derive a stable 31-bit seed for one exploration method."""
    digest = zlib.crc32(f"{base_seed}:{method}".encode("utf-8"))
    return int(digest % (2**31 - 1))


def method_seeds(base_seed: int, methods: list[str]) -> dict[str, int]:
    return {method: method_seed(base_seed, method) for method in methods}


def set_torch_seed(seed: int) -> None:
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
