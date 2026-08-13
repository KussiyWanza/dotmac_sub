"""Emit and verify Sub's canonical release-bound product manifest.

The assembly owns the product code and installed capability declarations. The
published kernel owns the document shape, canonical bytes, digest, and parser.
This module is only the release adapter joining those two facts to ``VERSION``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from dotmac_kernel.product_manifest import ProductManifestError, ProductManifestSnapshot

from app.composition import SUB_ASSEMBLY


class ProductManifestReleaseError(RuntimeError):
    """The release inputs cannot produce or verify Sub's canonical manifest."""


def _product_version(version_file: Path) -> str:
    try:
        raw = version_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProductManifestReleaseError(
            f"cannot read VERSION file {version_file}: {exc}"
        ) from exc

    value = raw[:-1] if raw.endswith("\n") else raw
    if (
        not value
        or value != value.strip()
        or "\n" in value
        or raw not in {value, f"{value}\n"}
    ):
        raise ProductManifestReleaseError(
            "VERSION must contain exactly one non-empty trimmed line"
        )
    return value


def _expected_snapshot(version_file: Path) -> ProductManifestSnapshot:
    try:
        return ProductManifestSnapshot.from_assembly(
            SUB_ASSEMBLY,
            product_version=_product_version(version_file),
        )
    except ValueError as exc:
        raise ProductManifestReleaseError(
            f"Sub assembly cannot produce a product manifest: {exc}"
        ) from exc


def emit_product_manifest(
    *,
    output: Path,
    version_file: Path,
) -> ProductManifestSnapshot:
    """Write the exact kernel-canonical bytes for this Sub release."""

    if not output.parent.is_dir():
        raise ProductManifestReleaseError(
            f"product-manifest output directory does not exist: {output.parent}"
        )
    snapshot = _expected_snapshot(version_file)
    try:
        output.write_bytes(snapshot.to_json_bytes())
    except OSError as exc:
        raise ProductManifestReleaseError(
            f"cannot write product manifest {output}: {exc}"
        ) from exc
    return snapshot


def verify_product_manifest(
    *,
    path: Path,
    version_file: Path,
) -> ProductManifestSnapshot:
    """Require exact canonical bytes derived from the current assembly/version."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProductManifestReleaseError(
            f"cannot read product manifest {path}: {exc}"
        ) from exc
    try:
        observed = ProductManifestSnapshot.from_json_bytes(payload)
    except ProductManifestError as exc:
        raise ProductManifestReleaseError(f"invalid product manifest: {exc}") from exc

    expected = _expected_snapshot(version_file)
    if observed != expected or payload != expected.to_json_bytes():
        raise ProductManifestReleaseError(
            "product manifest does not match SUB_ASSEMBLY and VERSION"
        )
    return observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("emit", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--version-file", required=True, type=Path)
        target = "--output" if name == "emit" else "--path"
        command.add_argument(target, required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "emit":
        snapshot = emit_product_manifest(
            output=args.output,
            version_file=args.version_file,
        )
    else:
        snapshot = verify_product_manifest(
            path=args.path,
            version_file=args.version_file,
        )
    print(snapshot.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
