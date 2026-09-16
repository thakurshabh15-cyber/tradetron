"""Vercel deployment identity verification against a local Vite build.

Vite content-hashes every emitted asset, so:
    SAME FILENAME => SAME BYTES => SAME SOURCE + DEPS + ENV

Fetches deployed index.html, extracts __vite__mapDeps from BOTH the deployed
and local entry chunks, reconciles the complete chunk sets, and byte-compares
all shared assets.

Exit code: 0 = identity current, 1 = mismatch found, 2 = usage/network error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import ssl
import sys
import urllib.request
from pathlib import Path

CTX = ssl.create_default_context()
_JS_RE = re.compile(
    r'(?:<script[^>]+src=|<link[^>]+(?:as="script"|rel="modulepreload")'
    r'[^>]*href=)"(/assets/[^"]+)"')
_CSS_RE = re.compile(r'<link[^>]+rel="stylesheet"[^>]*href="(/assets/[^"]+)"')
_MAPDEPS_RE = re.compile(r'm\.f\|\|\(m\.f=\[(.*?)\]')

P: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    P.append((name, "PASS" if cond else "FAIL", detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail[:300]}")


def fetch_bytes(url: str, timeout: int = 30) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=CTX) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"  !! fetch failed {url}: {type(e).__name__}: {e}")
        return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def mapdeps_chunks(text: str) -> set[str]:
    m = _MAPDEPS_RE.search(text)
    if not m:
        return set()
    chunks: set[str] = set()
    for p in re.split(r"\s*,\s*", m.group(1)):
        p2 = p.strip().strip('"')
        if p2.startswith("assets/"):
            chunks.add(p2.removeprefix("assets/"))
    return chunks
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deploy-url", default="https://tradethrone.vercel.app")
    ap.add_argument("--dist", default="client/dist")
    args = ap.parse_args()
    deploy_url = args.deploy_url.rstrip("/")
    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"local dist not found: {dist}"); return 2
    assets_dir = dist / "assets"
    if not assets_dir.is_dir():
        print(f"local dist/assets not found: {assets_dir}"); return 2

    html = fetch_bytes(f"{deploy_url}/")
    if html is None:
        return 2
    html = html.decode(errors="replace")
    refs_entry = sorted(set(_JS_RE.findall(html)) | set(_CSS_RE.findall(html)))
    if not refs_entry:
        print("!! no /assets/ refs parsed - abort"); return 2

    entry_name = next((r for r in refs_entry if r.endswith(".js") and "/index-" in r), None)
    local_entry_name = next(
        (n for n in assets_dir.iterdir() if n.is_file()
         and n.name.endswith(".js") and n.name.startswith("index-")), None)

    deployed_chunks: set[str] = set()
    if entry_name:
        et = fetch_bytes(f"{deploy_url}{entry_name}")
        if et:
            deployed_chunks = mapdeps_chunks(et.decode(errors="replace"))
            print(f"deployed {entry_name} ({len(et)}B): "
                  f"{len(deployed_chunks)} mapDeps chunks")
    local_chunks: set[str] = set()
    if local_entry_name:
        lb = local_entry_name.read_bytes()
        local_chunks = mapdeps_chunks(lb.decode(errors="replace"))
        print(f"local  {local_entry_name.name} ({len(lb)}B): "
              f"{len(local_chunks)} mapDeps chunks")

    deployed_set = set(refs_entry) | {f"/assets/{c}" for c in deployed_chunks}
    deployed_set = {r.removeprefix("/assets/") for r in deployed_set}
    local_set = {p.name for p in assets_dir.iterdir() if p.is_file()}
    if not deployed_chunks and entry_name:
        print("!! __vite__mapDeps missing in deployed entry")
    if not local_chunks and local_entry_name:
        print("!! __vite__mapDeps missing in local entry")

    print(f"\ndeployed bundle: {len(deployed_set)} assets")
    print(f"local  dist:     {len(local_set)} assets")

    shared = sorted(deployed_set & local_set)
    deploy_only = sorted(deployed_set - local_set)
    local_only = sorted(local_set - deployed_set)
    print(f"SHARED ({len(shared)}):")
    for s in shared:
        print(f"    {s}")
    print(f"DEPLOY-ONLY ({len(deploy_only)}): {deploy_only}")
    print(f"LOCAL-ONLY  ({len(local_only)}): {local_only}")

    shared_mismatch: list[tuple[str, str]] = []
    for name in shared:
        lb = (assets_dir / name).read_bytes()
        db = fetch_bytes(f"{deploy_url}/assets/{name}")
        if db is None:
            shared_mismatch.append((name, "unfetchable")); continue
        lh, dh = sha256(lb), sha256(db)
        if lh != dh:
            shared_mismatch.append((name, f"local={lh} deployed={dh}"))
        else:
            print(f"    OK {name} sha256={lh}")

    check("shared assets byte-identical", not shared_mismatch,
          "; ".join(f"{n}:{d}" for n, d in shared_mismatch))
    entry_js = {r for r in refs_entry if r.endswith(".js")
                and r.split("/")[-1].startswith("index-")}
    local_entry_js = {f"/assets/{n}" for n in local_set
                      if n.startswith("index-") and n.endswith(".js")}
    check("same index entry", entry_js == local_entry_js,
          f"deployed={entry_js} local={local_entry_js}")
    check("no local-only chunks (not stale)", not local_only)
    css_d = {r for r in refs_entry if r.endswith(".css")}
    css_l = {f"/assets/{n}" for n in local_set if n.endswith(".css")}
    check("same css", css_d == css_l, f"d={css_d} l={css_l}")

    verdict = (not shared_mismatch) and (not local_only) and (entry_js == local_entry_js)
    print(f"\n=== VERDICT: "
          f"{'CURRENT' if verdict else 'STALE - rebuild/redeploy required'} ===")
    for name, s, d in P:
        if s == "FAIL":
            print(f"  FAIL {name}: {d[:300]}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())