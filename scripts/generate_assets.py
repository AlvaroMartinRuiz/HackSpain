"""Draw the console's icon set with Quiver, once.

Credits are finite and a redraw of an icon that did not change is a wasted one,
so every asset is keyed by the hash of its prompt and style. A second run costs
nothing and prints "cached"; editing a prompt in src/design/assets.py is what
makes that one — and only that one — cost again.

    python scripts/generate_assets.py --list           # the catalogue, no key needed
    python scripts/generate_assets.py --models         # what this key may use
    python scripts/generate_assets.py --group status   # draw four icons
    python scripts/generate_assets.py                  # draw whatever is missing
    python scripts/generate_assets.py --only mark-socket-wizard --force
    python scripts/generate_assets.py --animate        # motion inside the status icons

The console works with none of this: without the files it falls back to CSS
shapes, so a missing key is never what breaks the demo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# PowerShell's default code page mangles the em dashes these prompts are full of.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config import settings  # noqa: E402
from src.design import assets as catalogue  # noqa: E402
from src.design.assets import Asset  # noqa: E402
from src.design.quiver import (  # noqa: E402
    QuiverClient, QuiverError, hardcoded_colours, themeable,
)

ASSET_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "assets"
MANIFEST = ASSET_DIR / "manifest.json"

# Only the live states earn motion; a calendar with a check mark on it has
# nothing to animate and would just cost a credit to find that out.
ANIMATE = {
    "status-listening": "Make the arcs ripple outwards from the circle in a slow, calm loop.",
    "status-thinking": "Rotate the gear slowly and clockwise, and fade the three dots in and out in sequence.",
    "status-speaking": "Make the bars rise and fall out of step with each other, like a level meter.",
}


def fingerprint(asset: Asset) -> str:
    material = f"{asset.prompt}\n{asset.style}\n{asset.width}x{asset.height}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def load_manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        return {"model": None, "credits_spent": 0, "generated": {}}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except ValueError:
        return {"model": None, "credits_spent": 0, "generated": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def is_current(asset: Asset, manifest: dict[str, Any], animate: bool) -> bool:
    record = manifest.get("generated", {}).get(asset.name)
    if not record or record.get("hash") != fingerprint(asset):
        return False
    if not (ASSET_DIR / f"{asset.name}.svg").exists():
        return False
    # Asking for motion on an icon that was drawn without it is still work to do.
    if animate and asset.name in ANIMATE and not record.get("animated"):
        return False
    return True


def draw_one(
    client: QuiverClient,
    asset: Asset,
    model: str,
    animate: bool,
) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    drawing = client.draw(
        prompt=asset.prompt,
        model=model,
        instructions=asset.style,
        width=asset.width,
        height=asset.height,
    )
    credits = drawing.credits
    animated = False

    if animate and asset.name in ANIMATE:
        try:
            motion = client.animate(drawing.svg, model=model, prompt=ANIMATE[asset.name])
        except QuiverError as exc:
            if not exc.unsupported:
                raise
            notes.append(f"animation unavailable on this account ({exc.code}); CSS motion instead")
        else:
            drawing, animated = motion, True
            credits += motion.credits

    svg, notes_from_fix = tidy(drawing.svg)
    notes.extend(notes_from_fix)

    # Created here, not when the manifest is first written: the drawing is
    # already paid for by this point, and losing it to a missing directory
    # means paying for it again.
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / f"{asset.name}.svg").write_text(svg + "\n", encoding="utf-8")

    return {
        "hash": fingerprint(asset),
        "group": asset.group,
        "model": drawing.model,
        "credits": credits,
        "tokens": drawing.total_tokens,
        "animated": animated,
        "request_id": drawing.request_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": len(svg),
    }, notes


def tidy(svg: str) -> tuple[str, list[str]]:
    """Make one drawing themeable, and say what had to be changed."""
    out, fixes = themeable(svg)
    notes: list[str] = []
    if fixes["plates"]:
        notes.append(f"dropped {fixes['plates']} background plate")
    if fixes["paints"]:
        notes.append(f"repainted {fixes['paints']} colours as currentColor")
    left = hardcoded_colours(out)
    if left:
        notes.append(f"still fixed, will not follow the theme: {', '.join(left[:4])}")
    return out, notes


def normalize_on_disk() -> int:
    """Re-tidy the SVGs already saved, without calling the API.

    Free, so it is the way to repair a set drawn before the tidier existed
    rather than paying to draw it all again.
    """
    files = sorted(ASSET_DIR.glob("*.svg")) if ASSET_DIR.is_dir() else []
    if not files:
        print(f"nothing in {ASSET_DIR}")
        return 1
    for path in files:
        before = path.read_text(encoding="utf-8")
        after, notes = tidy(before)
        changed = after.strip() != before.strip()
        if changed:
            path.write_text(after + "\n", encoding="utf-8")
        state = "changed" if changed else "already fine"
        print(f"  {path.stem:22} {state}")
        for note in notes:
            print(f"  {'':22}   ! {note}")
    print(f"\n{len(files)} file(s) checked, no credits spent")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the catalogue and exit")
    parser.add_argument("--models", action="store_true", help="print the models this key may use")
    parser.add_argument("--normalize", action="store_true",
                        help="re-tidy the SVGs already on disk, spending nothing")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="draw these assets by name")
    parser.add_argument("--group", nargs="+", metavar="GROUP", choices=catalogue.GROUPS,
                        help=f"draw these groups: {', '.join(catalogue.GROUPS)}")
    parser.add_argument("--force", action="store_true", help="redraw even what is already current")
    parser.add_argument("--animate", action="store_true",
                        help="put motion in the live status icons, if the account allows it")
    parser.add_argument("--model", help="override the model instead of asking the key")
    parser.add_argument("--yes", action="store_true", help="do not ask before spending credits")
    args = parser.parse_args()

    if args.list:
        for group in catalogue.GROUPS:
            rows = [a for a in catalogue.CATALOGUE if a.group == group]
            print(f"\n{group} ({len(rows)})")
            for asset in rows:
                print(f"  {asset.name:22} {asset.width}x{asset.height}  {asset.prompt[:66]}…")
        print(f"\n{len(catalogue.CATALOGUE)} assets in total")
        return 0

    if args.normalize:
        # Deliberately before the client is built: this needs no key.
        return normalize_on_disk()

    unknown = set(args.only or []) - set(catalogue.BY_NAME)
    if unknown:
        print(f"no such asset: {', '.join(sorted(unknown))}", file=sys.stderr)
        print("run --list to see the catalogue", file=sys.stderr)
        return 2

    try:
        client = QuiverClient()
    except QuiverError as exc:
        print(f"{exc}", file=sys.stderr)
        print("\nThe console runs without it — the icons fall back to CSS shapes.", file=sys.stderr)
        return 2

    try:
        if args.models:
            available = client.models()
            print("models on this key:" if available else "this key lists no models")
            for name in available:
                print(f"  {name}")
            return 0

        try:
            model = client.pick_model(args.model)
        except QuiverError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2

        manifest = load_manifest()
        wanted = catalogue.select(args.only, args.group)
        todo = wanted if args.force else [a for a in wanted if not is_current(a, manifest, args.animate)]
        cached = len(wanted) - len(todo)

        print(f"model     {model}")
        print(f"assets    {len(wanted)} asked for, {cached} already current, {len(todo)} to draw")
        if args.animate:
            covered = [a.name for a in todo if a.name in ANIMATE]
            print(f"animation {len(covered)} of them, a second charge each")
        spent = manifest.get("credits_spent", 0)
        if spent:
            print(f"spent     {spent} credits so far")

        if not todo:
            print("\nnothing to do.")
            return 0

        if not args.yes:
            print(f"\nThis spends credits on {len(todo)} generation(s). Continue? [y/N] ", end="")
            if input().strip().lower() not in {"y", "yes"}:
                print("nothing drawn.")
                return 1

        print()
        drawn = 0
        failed: list[tuple[str, str]] = []
        for index, asset in enumerate(todo, start=1):
            label = f"[{index}/{len(todo)}] {asset.name:22}"
            print(f"{label} …", end="", flush=True)
            started = time.perf_counter()
            try:
                record, notes = draw_one(client, asset, model, args.animate)
            except QuiverError as exc:
                print(f"\r{label} failed — {exc}")
                failed.append((asset.name, exc.code))
                # An empty balance or a revoked key will not fix itself, and the
                # remaining assets would each be one more identical failure.
                if exc.out_of_credit or exc.status == 401:
                    print("\nstopping: nothing further can succeed.")
                    break
                continue

            manifest.setdefault("generated", {})[asset.name] = record
            manifest["model"] = model
            manifest["credits_spent"] = manifest.get("credits_spent", 0) + record["credits"]
            # Saved per asset, so an interrupted run keeps what it paid for.
            save_manifest(manifest)
            drawn += 1

            elapsed = time.perf_counter() - started
            motion = " +motion" if record["animated"] else ""
            print(f"\r{label} ok  {record['credits']} credit(s)  {elapsed:.1f}s{motion}")
            for note in notes:
                print(f"{' ' * len(label)}   ! {note}")

        print(f"\n{drawn} drawn, {cached} cached, {len(failed)} failed")
        print(f"credits spent in total: {manifest.get('credits_spent', 0)}")
        print(f"assets in {ASSET_DIR}")
        if failed:
            for name, code in failed:
                print(f"  failed: {name} ({code})")
            return 1
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
