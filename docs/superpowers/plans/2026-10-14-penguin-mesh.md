# StreamingTalker mesh retargeting implementation plan

Goal: add an optional rigged GLB character driven by the existing StreamingTalker facial vertex stream, preserving audio-clock playback and interruption behavior.

1. Verify upstream animation completion and export a browser-compatible copy of the rig. Keep original assets outside the source release. Record the omission of Blender fur curves and authored head motion.
2. Extend deployment profiles with paired same-origin mesh and retarget assets. Add schema and route tests before implementation; reject unconfigured paths and duplicate character resources.
3. Implement and test a deterministic lip-pair aperture mapper. Validate topology, numeric bounds and rig calibration; clamp to the physical jaw range. Reset on idle and interruption.
4. Load GLB using pinned, local Three.js with its license. Isolate loading epochs, dispose replaced resources, and block conversation start when the selected mesh is unavailable. Keep the existing head and portrait renderers.
5. Integrate the penguin as a separate deployment profile. Verify actual streamed speech, visible mouth movement, idle, disconnect, rapid switching and the existing characters in a browser.
6. Document configuration, upstream attribution and limitations. Review the source diff, run focused backend and frontend suites, and scan the export for private deployment data before publishing changes.
