"""Make NVIDIA libraries installed from pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)
visible to CTranslate2 without the user editing PATH / LD_LIBRARY_PATH."""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import sys

log = logging.getLogger(__name__)
_done = False


def preload() -> None:
    global _done
    if _done or sys.platform == "darwin":
        return
    _done = True
    try:
        import nvidia  # namespace package from the nvidia-* wheels
    except ImportError:
        # torch with CUDA bundles the same libraries; importing it loads them.
        try:
            import torch  # noqa: F401
        except Exception:
            pass
        return
    roots = list(getattr(nvidia, "__path__", []))
    for root in roots:
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            if os.name == "nt":
                d = os.path.join(root, sub, "bin")
                if os.path.isdir(d):
                    os.add_dll_directory(d)
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            else:
                for lib in sorted(glob.glob(os.path.join(root, sub, "lib", "lib*.so*"))):
                    try:
                        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    except OSError as e:
                        log.debug("preload %s failed: %s", lib, e)
