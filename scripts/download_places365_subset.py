from pathlib import Path
import random
import shutil
from typing import Dict, List

import tensorflow_datasets as tfds
from PIL import Image
from tqdm import tqdm


# Output dataset used by your training script
OUT_DIR = Path("thesis_image_dataset")

# Temporary/raw source folder
RAW_OUT = Path("thesis_image_dataset/raw_places365")

# Your CNN classes
CLASS_MAP: Dict[str, List[str]] = {
    "falls_or_nature": [
        "forest_path",
        "forest_road",
        "mountain",
        "valley",
        "river",
        "waterfall",
        "natural_history_museum",  # only keep if visually appropriate after review
    ],
    "accommodation": [
        "bedroom",
        "hotel_room",
        "lobby",
        "motel",
        "hotel",
    ],
    "dining": [
        "restaurant",
        "dining_room",
        "cafeteria",
        "coffee_shop",
        "food_court",
    ],
    "landmark_or_tourist_spot": [
        "church",
        "plaza",
        "courtyard",
        "park",
        "museum",
        "tower",
    ],
    "coastal_or_boulevard": [
        "beach",
        "coast",
        "ocean",
        "boardwalk",
        "harbor",
    ],
}

# Public image targets per class.
# These are public images only. Add Bayawan images manually after this.
TARGET_PUBLIC_COUNTS = {
    "train": 25,
    "validation": 4,
    "test": 2,
}

ALLOWED_EXT = ".jpg"


def ensure_folders():
    for split in ["train", "validation", "test"]:
        for cls in [
            "falls_or_nature",
            "accommodation",
            "dining",
            "landmark_or_tourist_spot",
            "coastal_or_boulevard",
            "unknown",
        ]:
            (OUT_DIR / split / cls).mkdir(parents=True, exist_ok=True)


def safe_category_name(name: str) -> str:
    return name.replace("/", "_").replace(" ", "_").lower()


def save_image_array(image_array, out_path: Path) -> bool:
    try:
        image = Image.fromarray(image_array)
        image = image.convert("RGB")
        image.save(out_path, quality=90)
        return True
    except Exception as exc:
        print(f"Failed saving {out_path}: {exc}")
        return False


def load_places365_split(split: str):
    # Places365-small is still large but TFDS lets us stream/take samples.
    # If this fails, your machine/internet may not support the download route.
    return tfds.load(
        "places365_small",
        split=split,
        shuffle_files=True,
        as_supervised=False,
        data_dir=str(RAW_OUT),
    )


def get_label_names():
    builder = tfds.builder("places365_small", data_dir=str(RAW_OUT))
    builder.download_and_prepare(download_config=tfds.download.DownloadConfig(
        manual_dir=str(RAW_OUT / "manual")
    ))
    info = builder.info
    names = info.features["label"].names
    return names


def collect_for_split(split: str, label_names: List[str]):
    print(f"\nLoading Places365 split: {split}")
    ds = load_places365_split(split)

    # Map label name -> index
    name_to_idx = {safe_category_name(name): idx for idx, name in enumerate(label_names)}

    for thesis_class, places_categories in CLASS_MAP.items():
        target_count = TARGET_PUBLIC_COUNTS[split]
        out_class_dir = OUT_DIR / split / thesis_class
        out_class_dir.mkdir(parents=True, exist_ok=True)

        category_indices = []
        for cat in places_categories:
            key = safe_category_name(cat)
            if key in name_to_idx:
                category_indices.append(name_to_idx[key])
            else:
                print(f"  Warning: Places365 category not found: {cat}")

        if not category_indices:
            print(f"  Skipping {thesis_class}: no matching Places365 labels found.")
            continue

        selected = []
        max_scan = 20000  # prevents endless scans
        scanned = 0

        for sample in tqdm(tfds.as_numpy(ds), desc=f"{split}:{thesis_class}"):
            scanned += 1
            label = int(sample["label"])

            if label in category_indices:
                selected.append(sample)

            if len(selected) >= target_count or scanned >= max_scan:
                break

        random.shuffle(selected)

        saved = 0
        for i, sample in enumerate(selected[:target_count], start=1):
            out_path = out_class_dir / f"places365_{split}_{thesis_class}_{i:03d}.jpg"
            if save_image_array(sample["image"], out_path):
                saved += 1

        print(f"  Saved {saved}/{target_count} public images for {split}/{thesis_class}")


def main():
    ensure_folders()

    print("Preparing Places365 labels. This may take time on first run.")
    try:
        label_names = get_label_names()
    except Exception as exc:
        print("\nCould not prepare Places365 using TFDS.")
        print("Reason:", exc)
        print("\nAlternative: manually download from the official Places365/Places2 site or use Openverse/Flickr CC.")
        return

    print(f"Loaded {len(label_names)} Places365 labels.")

    # TFDS split names may be train/validation/test
    # If test is unavailable in your local TFDS setup, skip it and use manual/public images.
    for split in ["train", "validation", "test"]:
        try:
            collect_for_split(split, label_names)
        except Exception as exc:
            print(f"Skipping split {split} due to error: {exc}")

    print("\nDone downloading Places365 subset.")
    print("Next: manually review images and remove wrong ones.")
    print("Then add Bayawan images into the correct train/validation/test folders.")


if __name__ == "__main__":
    main()
