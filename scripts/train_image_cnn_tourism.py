import argparse
import json
from collections import OrderedDict
from pathlib import Path

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
EXPECTED_CLASSES = [
    "falls_or_nature",
    "accommodation",
    "dining",
    "landmark_or_tourist_spot",
    "coastal_or_boulevard",
    "unknown",
]


def count_images(root: Path):
    counts = OrderedDict()
    total = 0
    for cls in EXPECTED_CLASSES:
        folder = root / cls
        n = 0
        if folder.exists():
            for p in folder.rglob("*"):
                if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
                    n += 1
        counts[cls] = n
        total += n
    return counts, total


def main():
    parser = argparse.ArgumentParser(description="Train image CNN tourism prototype (MobileNetV2 transfer learning).")
    parser.add_argument("--dataset", default="thesis_image_dataset", help="Dataset root path")
    parser.add_argument("--out", default="artifacts/image_cnn_tourism", help="Output artifact directory")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--image-size", type=int, default=224)
    args = parser.parse_args()

    dataset_root = Path(args.dataset)
    train_dir = dataset_root / "train"
    val_dir = dataset_root / "validation"
    test_dir = dataset_root / "test"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    train_counts, train_total = count_images(train_dir)
    val_counts, val_total = count_images(val_dir)
    test_counts, test_total = count_images(test_dir)

    print("Train image counts:")
    for k, v in train_counts.items():
        print(f"  - {k}: {v}")
    print("Validation image counts:")
    for k, v in val_counts.items():
        print(f"  - {k}: {v}")
    print("Test image counts:")
    for k, v in test_counts.items():
        print(f"  - {k}: {v}")

    if train_total == 0 or val_total == 0:
        print("Dataset is missing or empty. Add images to train/ and validation/ first.")
        return 0

    try:
        import tensorflow as tf
        import numpy as np
    except Exception as exc:
        print(f"TensorFlow runtime unavailable: {exc}")
        return 1

    image_size = (int(args.image_size), int(args.image_size))

    train_ds = tf.keras.utils.image_dataset_from_directory(
        train_dir,
        labels="inferred",
        label_mode="int",
        class_names=EXPECTED_CLASSES,
        image_size=image_size,
        batch_size=args.batch_size,
        shuffle=True,
    )
    val_ds = tf.keras.utils.image_dataset_from_directory(
        val_dir,
        labels="inferred",
        label_mode="int",
        class_names=EXPECTED_CLASSES,
        image_size=image_size,
        batch_size=args.batch_size,
        shuffle=False,
    )

    autotune = tf.data.AUTOTUNE
    train_ds = train_ds.prefetch(autotune)
    val_ds = val_ds.prefetch(autotune)

    base_model = tf.keras.applications.MobileNetV2(
        input_shape=(image_size[0], image_size[1], 3),
        include_top=False,
        weights="imagenet",
    )
    base_model.trainable = False

    inputs = tf.keras.Input(shape=(image_size[0], image_size[1], 3))
    x = tf.keras.applications.mobilenet_v2.preprocess_input(inputs)
    x = base_model(x, training=False)
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    x = tf.keras.layers.Dropout(0.2)(x)
    outputs = tf.keras.layers.Dense(len(EXPECTED_CLASSES), activation="softmax")(x)
    model = tf.keras.Model(inputs, outputs)

    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=1e-3),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )

    callbacks = [
        tf.keras.callbacks.EarlyStopping(monitor="val_accuracy", patience=3, restore_best_weights=True),
    ]

    history = model.fit(train_ds, validation_data=val_ds, epochs=int(args.epochs), callbacks=callbacks, verbose=1)

    model_path = out_dir / "image_cnn_tourism.keras"
    model.save(model_path)

    label_map = {str(i): cls for i, cls in enumerate(EXPECTED_CLASSES)}
    (out_dir / "label_map.json").write_text(json.dumps(label_map, indent=2), encoding="utf-8")

    config = {
        "model_type": "MobileNetV2_transfer_learning",
        "input_size": [image_size[0], image_size[1], 3],
        "image_size": [image_size[0], image_size[1]],
        "classes": EXPECTED_CLASSES,
        "train_total": train_total,
        "validation_total": val_total,
        "test_total": test_total,
        "epochs_requested": int(args.epochs),
        "batch_size": int(args.batch_size),
    }
    (out_dir / "image_model_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    val_acc = float(max(history.history.get("val_accuracy", [0.0])))
    summary = {
        "val_accuracy_best": val_acc,
        "history": {k: [float(x) for x in v] for k, v in history.history.items()},
        "counts": {
            "train": train_counts,
            "validation": val_counts,
            "test": test_counts,
        },
    }

    if test_total > 0:
        test_ds = tf.keras.utils.image_dataset_from_directory(
            test_dir,
            labels="inferred",
            label_mode="int",
            class_names=EXPECTED_CLASSES,
            image_size=image_size,
            batch_size=args.batch_size,
            shuffle=False,
        ).prefetch(autotune)
        loss, acc = model.evaluate(test_ds, verbose=0)
        summary["test_loss"] = float(loss)
        summary["test_accuracy"] = float(acc)

        y_true = []
        y_pred = []
        for images, labels in test_ds:
            probs = model.predict(images, verbose=0)
            y_true.extend(labels.numpy().tolist())
            y_pred.extend(np.argmax(probs, axis=1).tolist())

        cm = tf.math.confusion_matrix(y_true, y_pred, num_classes=len(EXPECTED_CLASSES)).numpy()
        cm_path = out_dir / "confusion_matrix.csv"
        with cm_path.open("w", encoding="utf-8") as f:
            f.write("," + ",".join(EXPECTED_CLASSES) + "\n")
            for idx, row in enumerate(cm):
                f.write(EXPECTED_CLASSES[idx] + "," + ",".join(str(int(v)) for v in row) + "\n")

    (out_dir / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Saved model to: {model_path}")
    print(f"Best validation accuracy: {val_acc:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
