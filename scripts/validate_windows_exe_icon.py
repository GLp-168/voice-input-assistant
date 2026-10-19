"""Verify that a Windows executable embeds every frame from the product ICO."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct

import pefile


RT_ICON = 3
RT_GROUP_ICON = 14


def _ico_frame_digests(path: Path) -> set[str]:
    payload = path.read_bytes()
    if len(payload) < 6:
        raise ValueError(f"invalid ICO header: {path}")
    reserved, image_type, count = struct.unpack_from("<HHH", payload)
    if reserved != 0 or image_type != 1 or count <= 0:
        raise ValueError(f"invalid ICO directory: {path}")
    directory_end = 6 + count * 16
    if directory_end > len(payload):
        raise ValueError(f"truncated ICO directory: {path}")

    digests: set[str] = set()
    for index in range(count):
        entry_offset = 6 + index * 16
        image_size, image_offset = struct.unpack_from("<II", payload, entry_offset + 8)
        image_end = image_offset + image_size
        if image_size <= 0 or image_offset < directory_end or image_end > len(payload):
            raise ValueError(f"invalid ICO frame {index}: {path}")
        digests.add(hashlib.sha256(payload[image_offset:image_end]).hexdigest())
    return digests


def _resource_payloads(executable: Path, resource_type: int) -> list[bytes]:
    pe = pefile.PE(str(executable), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        resources = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        if resources is None:
            return []
        payloads: list[bytes] = []
        for type_entry in resources.entries:
            if type_entry.id != resource_type or not hasattr(type_entry, "directory"):
                continue
            for name_entry in type_entry.directory.entries:
                if not hasattr(name_entry, "directory"):
                    continue
                for language_entry in name_entry.directory.entries:
                    data = getattr(language_entry, "data", None)
                    if data is None:
                        continue
                    rva = data.struct.OffsetToData
                    size = data.struct.Size
                    payloads.append(pe.get_data(rva, size))
        return payloads
    finally:
        pe.close()


def validate(source_icon: Path, executable: Path) -> int:
    expected = _ico_frame_digests(source_icon)
    icon_payloads = _resource_payloads(executable, RT_ICON)
    group_payloads = _resource_payloads(executable, RT_GROUP_ICON)
    actual = {hashlib.sha256(payload).hexdigest() for payload in icon_payloads}
    missing = expected - actual
    if not group_payloads:
        raise ValueError(f"executable has no icon group: {executable}")
    if missing:
        raise ValueError(
            f"executable is missing {len(missing)} of {len(expected)} icon frame(s): "
            f"{executable}"
        )
    print(
        f"Windows executable icon validation passed: {executable} "
        f"({len(expected)} source frames)"
    )
    return len(expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_icon", type=Path)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    try:
        validate(args.source_icon.resolve(), args.executable.resolve())
    except (OSError, ValueError, pefile.PEFormatError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
