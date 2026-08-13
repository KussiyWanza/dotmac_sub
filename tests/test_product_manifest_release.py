"""A Sub release emits one canonical manifest from its declared assembly."""

from __future__ import annotations

from pathlib import Path

import pytest
from dotmac_kernel import ProductManifestSnapshot

from app.composition import SUB_ASSEMBLY
from scripts.product_manifest import (
    ProductManifestReleaseError,
    emit_product_manifest,
    verify_product_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_emitter_writes_the_kernel_canonical_snapshot_for_sub(tmp_path: Path) -> None:
    output = tmp_path / "product-manifest.json"

    emitted = emit_product_manifest(
        output=output,
        version_file=ROOT / "VERSION",
    )

    expected = ProductManifestSnapshot.from_assembly(
        SUB_ASSEMBLY,
        product_version=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
    )
    assert emitted == expected
    assert emitted.product_code == "dotmac-sub"
    assert emitted.capability_codes == tuple(sorted(emitted.capability_codes))
    assert output.read_bytes() == expected.to_json_bytes()
    assert (
        verify_product_manifest(
            path=output,
            version_file=ROOT / "VERSION",
        )
        == expected
    )


def test_verifier_refuses_bytes_not_derived_from_the_release_assembly(
    tmp_path: Path,
) -> None:
    output = tmp_path / "product-manifest.json"
    emit_product_manifest(output=output, version_file=ROOT / "VERSION")
    output.write_bytes(output.read_bytes().replace(b"dotmac-sub", b"dotmac-erp"))

    with pytest.raises(ProductManifestReleaseError, match="does not match"):
        verify_product_manifest(path=output, version_file=ROOT / "VERSION")


@pytest.mark.parametrize(
    "raw_version",
    [" 7.173.6\n", "7.173.6 \n", "7.173.6\nextra\n", "\n"],
)
def test_version_file_is_read_but_not_repaired(
    tmp_path: Path,
    raw_version: str,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text(raw_version, encoding="utf-8")

    with pytest.raises(ProductManifestReleaseError, match="VERSION"):
        emit_product_manifest(
            output=tmp_path / "product-manifest.json",
            version_file=version_file,
        )


def test_emitter_refuses_a_missing_output_directory(tmp_path: Path) -> None:
    with pytest.raises(ProductManifestReleaseError, match="output directory"):
        emit_product_manifest(
            output=tmp_path / "missing" / "product-manifest.json",
            version_file=ROOT / "VERSION",
        )
