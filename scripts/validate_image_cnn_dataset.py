import argparse
import json
from collections import defaultdict
from pathlib import Path

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SPLITS = ("train", "validation", "test")
EXPECTED_CLASSES = (
    "falls_or_nature",
    "accommodation",
    "dining",
    "landmark_or_tourist_spot",
    "coastal_or_boulevard",
    "unknown",
)

MIN_WARN = {
    "train": 20,
    "validation": 5,
    "test": 5,
}

TARGET = {
    "train": 35,
    "validation": 8,
    "test": 7,
}


def _scan_class_folder(class_dir: Path):
    valid_files = []
    invalid_files = []
    if not class_dir.exists():
        return valid_files, invalid_files
    for p in class_dir.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in ALLOWED_EXTS:
            valid_files.append(p)
        else:
            invalid_files.append(p)
    return valid_files, invalid_files


def validate_dataset(dataset_root: Path):
    issues = []
    warnings = []
    split_counts = {split: {cls: 0 for cls in EXPECTED_CLASSES} for split in SPLITS}
    invalid_type_files = []
    filename_occurrences = defaultdict(list)

    if not dataset_root.exists():
        issues.append(f"Dataset root is missing: {dataset_root}")
        return split_counts, issues, warnings, invalid_type_files, filename_occurrences

    for split in SPLITS:
        split_dir = dataset_root / split
        if not split_dir.exists():
            issues.append(f"Missing split folder: {split_dir}")
            continue
        for cls in EXPECTED_CLASSES:
            class_dir = split_dir / cls
            if not class_dir.exists():
                issues.append(f"Missing class folder: {class_dir}")
                continue
            valid_files, invalid_files = _scan_class_folder(class_dir)
            split_counts[split][cls] = len(valid_files)
            invalid_type_files.extend(invalid_files)
            for f in valid_files:
                # Duplicate check based on filename across splits.
                filename_occurrences[f.name.lower()].append((split, cls, str(f)))

    for split in SPLITS:
        for cls in EXPECTED_CLASSES:
            count = split_counts[split][cls]
            if count == 0:
                warnings.append(f"Empty class folder: {split}/{cls}")
            if count < MIN_WARN[split]:
                warnings.append(
                    f"Low image count for {split}/{cls}: {count} (minimum warning threshold: {MIN_WARN[split]})"
                )

    for split in SPLITS:
        split_total = sum(split_counts[split].values())
        if split_total == 0:
            warnings.append(f"Split has no valid images: {split}")

    if invalid_type_files:
        warnings.append(
            f"Found {len(invalid_type_files)} file(s) with unsupported type; allowed: {sorted(ALLOWED_EXTS)}"
        )

    return split_counts, issues, warnings, invalid_type_files, filename_occurrences


def print_report(dataset_root: Path, split_counts, issues, warnings, invalid_type_files, filename_occurrences):
    print(f"Dataset root: {dataset_root}")
    print("\nImage counts per split/class:")
    for split in SPLITS:
        print(f"[{split}]")
        for cls in EXPECTED_CLASSES:
            count = split_counts[split][cls]
            target = TARGET[split]
            print(f"  - {cls}: {count} (target: {target})")

    duplicate_groups = []
    for name, refs in filename_occurrences.items():
        involved_splits = sorted({r[0] for r in refs})
        if len(involved_splits) > 1:
            duplicate_groups.append((name, refs))

    print("\nSummary:")
    print(f"- Issues: {len(issues)}")
    print(f"- Warnings: {len(warnings)}")
    print(f"- Invalid type files: {len(invalid_type_files)}")
    print(f"- Duplicate filenames across splits: {len(duplicate_groups)}")

    if issues:
        print("\nIssues:")
        for item in issues:
            print(f"  * {item}")

    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"  * {item}")

    if invalid_type_files:
        print("\nUnsupported file types:")
        for p in invalid_type_files[:30]:
            print(f"  * {p}")
        if len(invalid_type_files) > 30:
            print(f"  ... and {len(invalid_type_files) - 30} more")

    if duplicate_groups:
        print("\nDuplicate filenames across splits:")
        for name, refs in duplicate_groups[:30]:
            print(f"  * {name}")
            for split, cls, path in refs:
                print(f"      - {split}/{cls}: {path}")
        if len(duplicate_groups) > 30:
            print(f"  ... and {len(duplicate_groups) - 30} more duplicate filename groups")


def main():
    parser = argparse.ArgumentParser(description="Validate image CNN dataset readiness (no training).")
    parser.add_argument("--dataset", default="thesis_image_dataset", help="Dataset root path")
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to save a JSON validation summary",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset)
    split_counts, issues, warnings, invalid_type_files, filename_occurrences = validate_dataset(dataset_root)
    print_report(dataset_root, split_counts, issues, warnings, invalid_type_files, filename_occurrences)

    if args.json_out:
        duplicate_groups = []
        for name, refs in filename_occurrences.items():
            involved_splits = sorted({r[0] for r in refs})
            if len(involved_splits) > 1:
                duplicate_groups.append({"filename": name, "occurrences": refs})
        payload = {
            "dataset_root": str(dataset_root),
            "allowed_exts": sorted(ALLOWED_EXTS),
            "splits": list(SPLITS),
            "classes": list(EXPECTED_CLASSES),
            "counts": split_counts,
            "issues": issues,
            "warnings": warnings,
            "invalid_type_files": [str(p) for p in invalid_type_files],
            "duplicate_filenames_across_splits": duplicate_groups,
            "min_warn_thresholds": MIN_WARN,
            "target_counts": TARGET,
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nSaved JSON report to: {out_path}")

    # Exit non-zero only for structural issues; warnings keep success for readiness checks.
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
