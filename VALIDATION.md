# Offline beta validation — 0.2.0

Executed on October 1, 2026, on macOS with Maya 2025.3.2 / Arnold 7.3.5.1 (MtoA 5.4.8.2).

- 57 core Python tests pass: native-unit/camera/up-axis planning, distinct validated choices, bounded refinement, cancellation, cached switching, scene-change guards, and renderer-failure handling.
- 24 native Maya adapter checks pass on disposable scenes: ownership, atomic replacement, Undo, camera requirements, mesh/material/animation preservation, local Qt image measurements, renderer diagnostics and watermark rejection.
- 6 Maya UI lifecycle tests pass: new/open/reopen invalidation, rejection of old queued work/results, and preservation of choices during save/scrubbing.
- A separate Maya/Arnold process renders all three local looks while Python network connections and external-process launching are blocked. Artist data, Original, and the saved/reopened editable rig are verified.
- The installed panel shows Offline / 0.2.0. The UI-only update preserves the current artist scene, dirty state, nodes, time, selection and playback range.

The standalone render environment produces native Arnold license watermarks. Those images are not used for automatic exposure assessment. Qt exposure analysis is independently tested with local, unwatermarked synthetic PNGs. Rendered lighting was visually inspected, but this does not establish artistic approval on a production shot.

Other Maya versions, operating systems, complex interiors and production-scale performance remain unverified. No production animation was changed or saved during this update.

## Reproduce

From the extracted folder:

```sh
python3 -m unittest discover -s tests -q
```

Run native tests with Maya's `mayapy`, using a separate `MAYA_APP_DIR` and disposable scenes. Run `examples/offline_validation.py --output /path/to/scratch` only in a separate mayapy process: it creates and replaces test scenes. Never execute that example in an open artist scene.
