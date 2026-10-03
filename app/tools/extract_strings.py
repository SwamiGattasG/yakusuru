"""Collect every interface string into yakusuru/locales/_template.json and report what each
translation file is missing.   Run from app/:  python tools/extract_strings.py"""
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PKG = ROOT / "yakusuru"


def from_code() -> set[str]:
    keys = set()
    for f in PKG.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("_", "_t") and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    keys.add(a.value)
    return keys


def from_data() -> set[str]:
    """Texts translated at runtime from data tables (passed to _() through a variable)."""
    from yakusuru import deps, hardware, languages, models
    from yakusuru.gui import queue_model
    keys = set(models.ENGINES.values()) | set(models.TRANSLATORS.values()) | set(models.CONTENT_TYPES.values())
    keys |= {m.notes for ms in models.ASR_MODELS.values() for m in ms if m.notes}
    keys |= set(queue_model.STATE_LABEL.values()) | set(queue_model.QueueModel.COLS)
    keys |= set(getattr(hardware, "PROFILES", {}).values())
    keys |= {l.name for l in languages._ALL}
    for osk in ("macos", "windows", "linux"):          # every OS/CPU combination's component texts
        for arch in ("arm64", "x86_64"):
            hw = hardware.HardwareInfo(os=osk, os_version="", arch=arch, cpu="", ram_gb=16, python="3.13")
            for prof in hardware.PROFILES:
                for c in deps.components_for(prof, hw):
                    keys |= {c.title, c.description} | ({c.unavailable_reason} if c.unavailable_reason else set())
    keys |= {"waiting on the network", "waiting on the network (no data arriving)", "reading model weights from disk",
             "running on the Apple GPU", "working (MLX)", "computing", "idle (no CPU, disk or network activity)",
             "waiting for an external tool (ffmpeg / whisper.cpp)",
             "waiting for a file lock on the model cache (another app or Yakusuru window may be using it)"}
    return keys


def main():
    keys = sorted(k for k in (from_code() | from_data()) if k.strip())
    loc = PKG / "locales"
    (loc / "_template.json").write_text(json.dumps({k: "" for k in keys}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    print(f"{len(keys)} strings → locales/_template.json")
    for f in sorted(loc.glob("[a-z]*.json")):
        have = json.loads(f.read_text(encoding="utf-8"))
        missing = [k for k in keys if not have.get(k)]
        print(f"{f.stem}: {len(keys) - len(missing)}/{len(keys)} translated" + (f", missing {len(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
