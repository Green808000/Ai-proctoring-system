"""
Bias / fairness test harness for the DeepFace-based identity verification
added in Task 3 (see detection/face_authentication.py: build_embedding,
verify_face_deep, get_deepface_threshold).

WHAT THIS DOES
---------------
Runs the exact same verification path the app uses in production
(routes/proctor.py's /verify-identity calls verify_face_deep the same way
this script does) against a manifest of test pairs you collect, and
reports false-accept / false-reject rates broken down by condition
(lighting, skin tone, glasses, angle) — plus a threshold sweep so the
team can see how the 0.593 default behaves vs. alternatives.

This does NOT collect the test photos for you and does NOT decide the
auto-block-vs-review question. It produces the numbers; the
recommendation in the "Task 6 findings" section of the report is written
by a human once the numbers are in, not fabricated by this script.

HOW TO USE
----------
1. Collect real photos (see scripts/README_fairness_testing.md for what's
   needed and how to organize files).
2. Fill in scripts/fairness_manifest.csv — one row per test comparison.
   Each row is one (enrolled photo, live-attempt photo) pair, labeled
   "genuine" (same person) or "impostor" (different person), tagged with
   the conditions it represents.
3. Run:
       cd scripts
       python bias_fairness_test.py --manifest fairness_manifest.csv
4. Check the generated results.csv (per-row) and report.md (summary +
   threshold sweep) in the --outdir you pass (default: results/).

SELF-TEST (no real photos, no DeepFace install needed)
--------------------------------------------------------
   python bias_fairness_test.py --self-test
This runs the whole pipeline against synthetic embeddings just to prove
the harness itself works (manifest parsing, distance calc, aggregation,
threshold sweep, report generation) before you invest time collecting
real faces. It tells you nothing about real-world accuracy.
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

import cv2
import numpy as np

# Make the project root importable when this script is run from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REQUIRED_COLUMNS = [
    "pair_id", "enrolled_image", "test_image", "expected_match",
    "lighting", "skin_tone", "glasses", "angle", "notes",
]

# Threshold sweep range for the "how sensitive is this to the threshold
# choice" table. DEEPFACE_MISMATCH_THRESHOLD (0.593) is included
# automatically even if it doesn't land on this grid.
SWEEP_START, SWEEP_STOP, SWEEP_STEP = 0.20, 0.90, 0.02

CONDITION_COLUMNS = ["lighting", "skin_tone", "glasses", "angle"]


def load_manifest(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"Manifest is missing required column(s): {missing}. "
                f"Found columns: {reader.fieldnames}"
            )
        for i, row in enumerate(reader, start=2):  # start=2: header is line 1
            row = {k: (v or "").strip() for k, v in row.items()}
            if not row["pair_id"]:
                continue  # skip blank rows
            match = row["expected_match"].lower()
            if match not in ("genuine", "impostor"):
                raise ValueError(
                    f"Line {i}: expected_match must be 'genuine' or "
                    f"'impostor', got {row['expected_match']!r}"
                )
            rows.append(row)
    if not rows:
        raise ValueError("Manifest has no data rows.")
    return rows


def get_verification_functions(self_test):
    """Returns (build_embedding, verify_face_deep, default_threshold).

    In real mode these are the actual project functions, so this script
    exercises the identical code path production uses. In self-test mode
    they're swapped for synthetic stand-ins so the harness can be
    validated without DeepFace installed or real photos.
    """
    if not self_test:
        from detection.face_authentication import (
            build_embedding, verify_face_deep, get_deepface_threshold,
        )
        return build_embedding, verify_face_deep, get_deepface_threshold()

    # --- synthetic stand-ins for --self-test only ---
    rng = np.random.default_rng(42)
    identity_vectors = {}

    def _identity_key(path):
        # In self-test mode "image paths" are just synthetic labels like
        # person1_enrolled.jpg / person1_dim.jpg — same leading identity
        # token = same synthetic person.
        base = os.path.basename(path)
        return base.split("_")[0]

    def fake_build_embedding(image_bgr_or_path):
        key = _identity_key(image_bgr_or_path)
        if key not in identity_vectors:
            identity_vectors[key] = rng.normal(size=128)
        # small per-photo noise so repeated shots of the same person
        # aren't bit-identical, like real embeddings wouldn't be
        return identity_vectors[key] + rng.normal(scale=0.05, size=128)

    def fake_verify_face_deep(test_path, enrolled_embedding):
        live = fake_build_embedding(test_path)
        a = live / np.linalg.norm(live)
        b = enrolled_embedding / np.linalg.norm(enrolled_embedding)
        return float(1 - np.dot(a, b))

    return fake_build_embedding, fake_verify_face_deep, 0.593


def run(manifest_rows, build_embedding, verify_face_deep, self_test):
    enrolled_cache = {}
    results = []

    for row in manifest_rows:
        enrolled_path = row["enrolled_image"]
        test_path = row["test_image"]

        if enrolled_path not in enrolled_cache:
            if self_test:
                embedding = build_embedding(enrolled_path)
            else:
                image_bgr = cv2.imread(enrolled_path)
                if image_bgr is None:
                    print(f"[WARN] pair {row['pair_id']}: could not read "
                          f"enrolled image {enrolled_path!r}, skipping row")
                    continue
                embedding = build_embedding(image_bgr)
            if embedding is None:
                print(f"[WARN] pair {row['pair_id']}: no face found in "
                      f"enrolled image {enrolled_path!r}, skipping row")
                continue
            enrolled_cache[enrolled_path] = embedding

        enrolled_embedding = enrolled_cache[enrolled_path]

        if self_test:
            distance = verify_face_deep(test_path, enrolled_embedding)
        else:
            test_bgr = cv2.imread(test_path)
            if test_bgr is None:
                print(f"[WARN] pair {row['pair_id']}: could not read test "
                      f"image {test_path!r}, skipping row")
                continue
            distance = verify_face_deep(test_bgr, enrolled_embedding)

        if distance is None:
            print(f"[WARN] pair {row['pair_id']}: no face found in test "
                  f"image {test_path!r}, skipping row")
            continue

        results.append({**row, "distance": distance})

    return results


def classify(results, threshold):
    """Adds pass/error_type to each result at a given threshold. Returns
    a new list; does not mutate the input (so this can be called once per
    threshold in the sweep without re-running verification)."""
    out = []
    for r in results:
        passed = r["distance"] <= threshold
        genuine = r["expected_match"] == "genuine"
        if genuine and passed:
            error_type = "correct_accept"
        elif genuine and not passed:
            error_type = "false_reject"
        elif not genuine and not passed:
            error_type = "correct_reject"
        else:
            error_type = "false_accept"
        out.append({**r, "passed": passed, "error_type": error_type})
    return out


def write_results_csv(classified, path):
    fieldnames = REQUIRED_COLUMNS + ["distance", "passed", "error_type"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in classified:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def rate(classified, error_type, denom_types):
    denom = [r for r in classified if r["error_type"] in denom_types]
    if not denom:
        return None
    num = [r for r in denom if r["error_type"] == error_type]
    return len(num) / len(denom)


def overall_rates(classified):
    far = rate(classified, "false_accept", {"false_accept", "correct_reject"})
    frr = rate(classified, "false_reject", {"false_reject", "correct_accept"})
    return far, frr


def condition_breakdown(classified, column):
    groups = defaultdict(list)
    for r in classified:
        groups[r[column] or "(blank)"].append(r)
    breakdown = {}
    for value, rows in sorted(groups.items()):
        far, frr = overall_rates(rows)
        n_genuine = sum(1 for r in rows if r["expected_match"] == "genuine")
        n_impostor = sum(1 for r in rows if r["expected_match"] == "impostor")
        breakdown[value] = {
            "n_genuine": n_genuine,
            "n_impostor": n_impostor,
            "false_reject_rate": frr,
            "false_accept_rate": far,
        }
    return breakdown


def threshold_sweep(results, default_threshold):
    thresholds = sorted(set(
        [round(t, 2) for t in
         np.arange(SWEEP_START, SWEEP_STOP + 1e-9, SWEEP_STEP)]
        + [default_threshold]
    ))
    sweep = []
    for t in thresholds:
        classified = classify(results, t)
        far, frr = overall_rates(classified)
        sweep.append({"threshold": t, "far": far, "frr": frr})
    return sweep


def fmt_pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def write_report(classified, sweep, default_threshold, path, self_test):
    lines = []
    lines.append("# Task 6 — Bias/Fairness Test Report\n")
    if self_test:
        lines.append(
            "> **SELF-TEST MODE** — these numbers are from synthetic "
            "embeddings, not real faces. This only confirms the harness "
            "runs correctly. Re-run without --self-test on real photos "
            "before drawing any conclusions.\n"
        )

    n = len(classified)
    n_genuine = sum(1 for r in classified if r["expected_match"] == "genuine")
    n_impostor = n - n_genuine
    lines.append(f"Total comparisons: {n} ({n_genuine} genuine, "
                  f"{n_impostor} impostor)\n")

    far, frr = overall_rates(classified)
    lines.append("## Overall, at the current default threshold "
                  f"({default_threshold})\n")
    lines.append(f"- False accept rate (impostor let through): {fmt_pct(far)}")
    lines.append(f"- False reject rate (genuine student blocked): {fmt_pct(frr)}\n")

    lines.append("## By condition\n")
    for column in CONDITION_COLUMNS:
        lines.append(f"### {column.replace('_', ' ').title()}\n")
        lines.append("| Value | Genuine pairs | Impostor pairs | False reject rate | False accept rate |")
        lines.append("|---|---|---|---|---|")
        for value, stats in condition_breakdown(classified, column).items():
            lines.append(
                f"| {value} | {stats['n_genuine']} | {stats['n_impostor']} | "
                f"{fmt_pct(stats['false_reject_rate'])} | "
                f"{fmt_pct(stats['false_accept_rate'])} |"
            )
        lines.append("")

    lines.append("## Threshold sweep\n")
    lines.append(
        "Lower threshold = stricter (fewer false accepts, more false "
        "rejects). Higher threshold = looser (opposite). Current default "
        f"is **{default_threshold}**.\n"
    )
    lines.append("| Threshold | False accept rate | False reject rate |")
    lines.append("|---|---|---|")
    for row in sweep:
        marker = " *(current default)*" if row["threshold"] == default_threshold else ""
        lines.append(
            f"| {row['threshold']}{marker} | {fmt_pct(row['far'])} | "
            f"{fmt_pct(row['frr'])} |"
        )
    lines.append("")

    lines.append("## Findings (fill in once run on real data)\n")
    lines.append("- Conditions with a notably higher false-reject or "
                  "false-accept rate than the rest:")
    lines.append("  - _TODO_")
    lines.append("- Is any gap large enough / consistent enough to call a "
                  "bias problem rather than noise (small sample sizes will "
                  "look noisy — say so if that's the case)?")
    lines.append("  - _TODO_")
    lines.append("- Recommended threshold, if different from the current "
                  "default, and why:")
    lines.append("  - _TODO_")
    lines.append(
        "- Recommendation on auto-block vs. flag-for-human-review for a "
        "mismatch, based on the above:"
    )
    lines.append("  - _TODO_")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="fairness_manifest.csv",
                         help="Path to the manifest CSV (default: %(default)s)")
    parser.add_argument("--outdir", default="results",
                         help="Directory to write results.csv/report.md into "
                              "(default: %(default)s)")
    parser.add_argument("--self-test", action="store_true",
                         help="Run against synthetic data to validate the "
                              "harness itself, without DeepFace or real photos.")
    args = parser.parse_args()

    build_embedding, verify_face_deep, default_threshold = \
        get_verification_functions(args.self_test)

    if args.self_test and not os.path.exists(args.manifest):
        # Auto-generate a tiny synthetic manifest so --self-test works
        # with zero setup.
        args.manifest = _write_self_test_manifest()

    manifest_rows = load_manifest(args.manifest)
    print(f"Loaded {len(manifest_rows)} rows from {args.manifest}")

    results = run(manifest_rows, build_embedding, verify_face_deep, args.self_test)
    print(f"Got distances for {len(results)}/{len(manifest_rows)} rows "
          f"(rows with unreadable images or no detected face are skipped)")
    if not results:
        print("No usable results — nothing to report.")
        return

    classified = classify(results, default_threshold)
    sweep = threshold_sweep(results, default_threshold)

    os.makedirs(args.outdir, exist_ok=True)
    results_path = os.path.join(args.outdir, "results.csv")
    report_path = os.path.join(args.outdir, "report.md")
    write_results_csv(classified, results_path)
    write_report(classified, sweep, default_threshold, report_path, args.self_test)

    print(f"\nWrote {results_path}")
    print(f"Wrote {report_path}")

    far, frr = overall_rates(classified)
    print(f"\nOverall @ threshold {default_threshold}: "
          f"FAR={fmt_pct(far)}  FRR={fmt_pct(frr)}")


def _write_self_test_manifest():
    """Writes a small synthetic manifest for --self-test convenience and
    returns its path. Image paths here are just labels (see
    get_verification_functions's fake_build_embedding) — no real files
    are read in self-test mode."""
    path = "_self_test_manifest.csv"
    rows = [
        ("p1", "person1_enrolled.jpg", "person1_bright.jpg", "genuine", "bright", "light", "no", "front", ""),
        ("p2", "person1_enrolled.jpg", "person1_dim.jpg", "genuine", "dim", "light", "no", "front", ""),
        ("p3", "person1_enrolled.jpg", "person1_glasses.jpg", "genuine", "bright", "light", "yes", "front", ""),
        ("p4", "person1_enrolled.jpg", "person1_angled.jpg", "genuine", "bright", "light", "no", "side", ""),
        ("p5", "person2_enrolled.jpg", "person2_bright.jpg", "genuine", "bright", "dark", "no", "front", ""),
        ("p6", "person2_enrolled.jpg", "person2_dim.jpg", "genuine", "dim", "dark", "no", "front", ""),
        ("p7", "person1_enrolled.jpg", "person2_bright.jpg", "impostor", "bright", "mixed", "no", "front", ""),
        ("p8", "person2_enrolled.jpg", "person1_dim.jpg", "impostor", "dim", "mixed", "no", "front", ""),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REQUIRED_COLUMNS)
        writer.writerows(rows)
    return path


if __name__ == "__main__":
    main()
