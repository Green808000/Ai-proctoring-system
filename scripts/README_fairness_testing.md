# Task 6 — Bias/Fairness Testing

This folder has everything needed to test `verify_face_deep()` (the
DeepFace-based identity check from Task 3) across the conditions the
task brief calls out: lighting, skin tone, glasses, and angle.

## 1. What to collect

For each person in the test (aim for as many teammates/volunteers as you
can get, and prioritize variety in skin tone over just raw headcount):

- **One enrollment photo** — same kind of shot a student would upload at
  registration (clear, front-on, decent lighting). This is what gets
  compared against everything else for that person.
- **Several "live attempt" photos**, each deliberately varying ONE
  condition at a time where possible:
  - `lighting`: bright / normal / dim
  - `skin_tone`: use whatever categories the team is comfortable
    recording — could be self-reported, or Fitzpatrick I–VI buckets.
    Consistency matters more than which scale you pick.
  - `glasses`: yes / no
  - `angle`: front / slight turn / side profile

You also need a handful of **impostor pairs** — person A's enrollment
photo compared against person B's live photo — to measure false
accepts, not just false rejects. Without impostor pairs you can only
measure "does it recognize the right person," not "does it also reject
the wrong person," and both matter for the auto-block-vs-review call.

Save everything as jpg/png anywhere convenient; you'll reference full
paths in the manifest.

## 2. Fill in the manifest

Copy the header row below into `fairness_manifest.csv` in this folder
(or edit the one the script generates) and add one row per comparison:

```
pair_id,enrolled_image,test_image,expected_match,lighting,skin_tone,glasses,angle,notes
p1,photos/miracle_enrolled.jpg,photos/miracle_dim.jpg,genuine,dim,medium,no,front,
p2,photos/miracle_enrolled.jpg,photos/chetanna_bright.jpg,impostor,bright,medium,no,front,
```

- `expected_match` is `genuine` (same person) or `impostor` (different
  people) — this is ground truth you set, not something the script
  figures out.
- Leave a condition column blank if it doesn't apply / wasn't varied for
  that shot; the report will group blanks under "(blank)".
- `notes` is free text — useful for anything odd about that row
  (e.g. "webcam was low-res", "photo taken on phone not laptop").

## 3. Run it

```bash
cd scripts
python bias_fairness_test.py --manifest fairness_manifest.csv
```

This uses the actual project code (`build_embedding`, `verify_face_deep`,
`get_deepface_threshold` from `detection/face_authentication.py`), so
you'll need `deepface` installed in this environment (`pip install
deepface`) — the same dependency Task 3 already requires.

Output goes to `scripts/results/`:
- `results.csv` — one row per comparison, with the measured distance,
  pass/fail, and error type.
- `report.md` — false-accept/false-reject rates overall, broken down by
  each condition, plus a threshold sweep so you can see how sensitive
  the results are to the 0.593 default.

## 4. What to do with the output

The report ends with a "Findings" section with `_TODO_` placeholders —
that's deliberate. The script measures rates; it doesn't decide whether
a gap is a real bias problem or just noise from a small sample, and it
doesn't decide the auto-block-vs-review policy. That write-up is the
actual Task 6 deliverable to bring back to the team — fill it in based
on what the numbers show, and call out plainly if a category (e.g. too
few dark-skin-tone samples) is too small to conclude anything from
rather than forcing a conclusion out of thin data.

## Trying the harness first (optional)

```bash
python bias_fairness_test.py --self-test
```

Runs the whole pipeline against synthetic embeddings with zero setup, to
confirm the script itself works before you spend time collecting real
photos. It writes to the same `results/` folder and tells you nothing
about real-world accuracy — don't use its numbers for anything.
