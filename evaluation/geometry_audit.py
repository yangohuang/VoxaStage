"""Numerical geometry diagnostics for existing captures, not perceptual scores.

Run from the repository root: python -m evaluation.geometry_audit --help.
Whole-mesh motion includes pose and expression; it cannot assess phonetic sync.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from avatar_review import load_captures, write_exclusive

AREA_THRESHOLD = 1e-12


def measure_geometry(faces, frames):
    """Consume (PTS seconds, xyz array) pairs, retaining one previous mesh."""
    faces = np.asarray(faces)
    if (faces.ndim != 2 or faces.shape[1] != 3 or not 1 <= len(faces) <= 40000
            or faces.dtype.kind not in 'iu'):
        raise ValueError('invalid triangle indices')
    previous = None
    previous_pts = None
    minimum_area = float('inf')
    near_faces = near_frames = 0
    area_by_frame = []
    transitions = []
    count = 0
    for pts, values in frames:
        values = np.asarray(values, dtype=np.float64)
        if (values.ndim != 2 or values.shape[1] != 3 or not 3 <= len(values) <= 20000
                or not np.isfinite(values).all() or not np.isfinite(pts) or pts < 0
                or count >= 1801):
            raise ValueError('invalid frame')
        if previous is None:
            vertex_count = len(values)
            if faces.min() < 0 or faces.max() >= vertex_count:
                raise ValueError('invalid topology indices')
            scale = float(np.linalg.norm(np.ptp(values, axis=0)))
            if not np.isfinite(scale) or scale <= 0:
                raise ValueError('invalid first-frame scale')
        elif values.shape != previous.shape or pts <= previous_pts:
            raise ValueError('shape or clock changed')
        triangles = values[faces]
        areas = .5 * np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                             triangles[:, 2] - triangles[:, 0]), axis=1) / scale**2
        minimum_area = min(minimum_area, float(areas.min()))
        near = int(np.count_nonzero(areas <= AREA_THRESHOLD))
        near_faces += near
        near_frames += near > 0
        area_by_frame.append(dict(frame=count, pts=float(pts),
                                  minimum_normalized_triangle_area=float(areas.min()),
                                  near_degenerate_faces=near))
        if previous is not None:
            motion = float(np.sqrt(np.square(values - previous).sum(axis=1).mean()) / scale)
            transitions.append(dict(from_frame=count-1, to_frame=count, pts=float(pts),
                                    rms_displacement_over_bbox=motion))
        previous, previous_pts = values.copy(), pts
        count += 1
    if count == 0:
        raise ValueError('empty clip')
    motion = [t['rms_displacement_over_bbox'] for t in transitions]
    return dict(
        frames=count, vertices=vertex_count, faces=len(faces),
        first_frame_bbox_diagonal=scale,
        duplicate_faces_ignoring_winding=len(faces)-len(np.unique(np.sort(faces, axis=1), axis=0)),
        repeated_index_faces=int(np.count_nonzero((faces[:, 0] == faces[:, 1])
                                                  | (faces[:, 0] == faces[:, 2])
                                                  | (faces[:, 1] == faces[:, 2]))),
        unreferenced_vertices=vertex_count-len(np.unique(faces)),
        normalized_area_threshold=AREA_THRESHOLD,
        minimum_normalized_triangle_area=minimum_area,
        near_degenerate_face_frames=near_faces,
        frames_with_near_degenerate_faces=near_frames,
        area_by_frame=area_by_frame,
        motion=dict(pairs=len(motion), mean=float(np.mean(motion)) if motion else None,
                    p95=float(np.percentile(motion, 95)) if motion else None,
                    max=max(motion) if motion else None),
        largest_transitions=sorted(transitions, key=lambda t: -t['rms_displacement_over_bbox'])[:5],
    )


def audit(captures):
    clips = load_captures(captures)
    results = []
    for clip in clips:
        if clip.kind != '3d':
            continue
        geometry = measure_geometry(clip.faces, (
            (frame['pts'], np.frombuffer(clip.frame(i), dtype='<f4').reshape(-1, 3))
            for i, frame in enumerate(clip.index)))
        results.append(dict(provider=clip.provider, case=clip.case, category=clip.category,
                            fingerprint=clip.fingerprint, geometry=geometry))
    if not results:
        raise ValueError('no 3D captures')
    root = Path(__file__).resolve().parents[1]
    sources = ['evaluation/geometry_audit.py', 'avatar_review.py']
    return dict(schema=1, perceptual_quality_measured=False, lip_sync_measured=False,
                normalization='first-frame bounding-box diagonal, per clip',
                area_definition='triangle area divided by squared diagonal',
                motion_definition='RMS Euclidean per-vertex displacement divided by diagonal; includes rigid motion',
                source_sha256={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
                validated_captures=len(clips), measured_3d_captures=len(results), clips=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.captures)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_exclusive(args.output, result)
    print(json.dumps(dict(measured_3d_captures=result['measured_3d_captures'],
                          output=str(args.output))))
