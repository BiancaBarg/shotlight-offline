# ShotLight for Maya — Offline Beta 0.2.0

Automatic, editable Arnold lighting for a selected shot camera and animation range. Everything in ShotLight runs locally inside Maya. No cloud account, API key, external client, Python package installation, or uploaded scene is required.

**[Download offline beta 0.2.0](https://github.com/BiancaBarg/shotlight-offline/releases/tag/v0.2.0-beta.1)** · **[Share feedback or report a bug](https://github.com/BiancaBarg/shotlight-offline/issues/new/choose)**

Made by Bianca Bargan 😊

## Install

1. Extract the entire ZIP into a folder you intend to keep.
2. Open Maya, then drag `install.py` from that extracted folder into the viewport.
3. A **LIGHT / ShotLight** shelf button opens the panel. Keep the extracted folder in place: the button loads its code from that folder.

For an update, stop any ShotLight job and drag the new `install.py` into the viewport. The installer refreshes the loaded code and replaces the existing launcher.

Tested on Maya 2025.3.2 on macOS, with its bundled Arnold renderer and Qt. Other Maya versions and operating systems have not been verified. This beta targets Maya/Arnold only.

## Light a shot

1. **Select your shot camera in Maya.** The panel requires an explicit camera selection.
2. **Set the timeline playback start and end to that shot's range.**
3. Press **Light Shot**. The first choice, **Soft sculpted — Recommended**, creates native editable lights and a small preview.
4. Choose **Directional contrast** or **Warm studio** to apply another cached rig immediately. Click **Preview this version** to render it.
5. **Check shot (3 frames)** measures preview exposure at the start, middle and end, with at most one bounded exposure adjustment. This samples the animation; it does not inspect every frame.
6. **Original** removes only ShotLight-owned lights and restores the scene's previous lighting. Your existing artist lights remain. The three choices stay available.

The panel retains **Stop**, camera/range instructions, and the small creator credit. Scene changes invalidate old work and choices. Maya Undo also restores a previous rig.

## How this offline version decides

The engine uses the sampled camera orientation, the subjects' motion bounds, scene units and material brightness/roughness cues. Three camera-relative lighting strategies choose area-light positions, sizes, colours and key/fill/edge ratios. Environment bounds help avoid some emitter intersections. The lights cast native Arnold shadows and remain ordinary editable Maya nodes.

One small baseline render establishes initial balance. One selected-version preview follows. A local pixel check may make one exposure adjustment and render once more. Version switching requires no render or connection. Final render settings are restored after previews.

This is a **local rule-based lighting engine**, not a trained vision model. Screen bounds and exposure measurements are approximate: they cannot understand facial expression, story, artistic intent, textured reflectance or exact occlusion. Complex interiors, transparent objects, large camera moves and unusual materials may require manual edits. A technical exposure check is not artistic approval.

Generation and sampled checks have a 90-second budget; individual quick renders request abort after 15 seconds. Stop also requests abort. Maya/Arnold may take additional time to finish native cleanup. ShotLight never force-quits Maya.

If Arnold reports a license watermark, the preview can still be shown but automatic exposure tuning and technical assessment are disabled. Normal Maya/Arnold licensing still applies.

## Beta feedback

Please try the beta on a copy of a shot and tell me what worked, what looked wrong, and what would make it useful in your workflow. **[Send feedback](https://github.com/BiancaBarg/shotlight-offline/issues/new/choose)**. Useful details include:

- Maya/Arnold version and operating system.
- Whether installation and explicit camera selection worked.
- The look chosen, elapsed time, and whether shadows/subject readability improved.
- Unexpected changes, errors, or difficulty restoring Original.

Review any images or logs before sharing them, and use a disposable scene for reproduction. See `VALIDATION.md` for the tested scope.
