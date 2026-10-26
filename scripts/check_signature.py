"""Check a signed plugin bundle the way a hub does before it records it: the signature beside the bundle, against the
publishers an operator would list in EVO_HUB_PLUGIN_PUBLISHERS.

    python scripts/check_signature.py dist/lumen-hello-0.1.0.tar.gz \
        --publishers "lumen=sigstore:OWNER/REPO/.github/workflows/ci.yml"

The signature is BUNDLE.sigstore.json (what `sigstore sign BUNDLE` writes) or BUNDLE.sig.json (what
`evo-agents plugin sign` writes), or --signature FILE. Needs evo-ak; a Sigstore signature also needs the sigstore
package, which fetches Sigstore's public trust root. Exit 0 when the hub would accept the signature, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.signing import (
    SignatureError,
    SignatureUnavailable,
    parse_publishers,
    read_signature,
    sha256_of,
    verify,
)

SUFFIXES = (".sigstore.json", ".sig.json")


def signature_path(bundle: Path, given: str | None) -> Path:
    if given:
        return Path(given)
    for suffix in SUFFIXES:
        candidate = bundle.with_name(bundle.name + suffix)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no {' or '.join(bundle.name + suffix for suffix in SUFFIXES)} beside the bundle")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--publishers", required=True, help="EVO_HUB_PLUGIN_PUBLISHERS entries, comma-separated")
    parser.add_argument("--signature", help="the signature file (default: beside the bundle)")
    args = parser.parse_args(argv)
    try:
        data = args.bundle.read_bytes()
        manifest = read_bundle(data).manifest
        path = signature_path(args.bundle, args.signature)
        envelope = read_signature(path.read_bytes())
        verified = verify(data, envelope, manifest.publisher, parse_publishers(args.publishers))
    except SignatureError as exc:  # before ValueError, which it extends
        print(f"refused ({exc.reason}): {exc}", file=sys.stderr)
        return 1
    except (OSError, BundleError, ValueError, SignatureUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.bundle.name}: {manifest.id} {manifest.version}, sha256 {sha256_of(data)}")
    print(f"accepted: publisher {verified.publisher}, {verified.kind}, signed by {verified.signed_by} ({path.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
