from __future__ import annotations

import argparse
import json
from pathlib import Path

from formats import sha256_file


ROOT = Path(__file__).resolve().parents[1]


def load_config() -> dict:
    return json.loads((ROOT / "config" / "inputs.json").read_text(encoding="utf-8"))


def check(path: Path, expected: dict) -> dict:
    if not path.is_file():
        raise SystemExit(f"missing input: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected["size"]:
        raise SystemExit(
            f"size mismatch: {path}\nexpected {expected['size']}\nactual   {actual_size}"
        )
    actual_sha = sha256_file(path)
    if actual_sha.lower() != expected["sha256"].lower():
        raise SystemExit(
            f"sha256 mismatch: {path}\nexpected {expected['sha256']}\nactual   {actual_sha}"
        )
    return {"path": str(path), "size": actual_size, "sha256": actual_sha}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="also verify large Windows voice containers")
    args = parser.parse_args()

    cfg = load_config()
    results = []
    ps2 = (ROOT / cfg["ps2_iso"]["path"]).resolve()
    results.append(check(ps2, cfg["ps2_iso"]))

    win_root = (ROOT / cfg["windows"]["root"]).resolve()
    required = ["data.pak", "deji.exe"]
    if args.full:
        required += ["voice.pak", "voice2.pak"]
    for name in required:
        results.append(check(win_root / name, cfg["windows"]["files"][name]))

    print(json.dumps({"ok": True, "inputs": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
