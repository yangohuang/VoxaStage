import unittest

import numpy as np

from evaluation.geometry_audit import measure_geometry


class GeometryAuditTests(unittest.TestCase):
    def setUp(self):
        self.vertices = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
        self.faces = np.array([[0, 1, 2]])

    def measure(self, frames, faces=None):
        return measure_geometry(self.faces if faces is None else faces,
                                ((i / 30, v) for i, v in enumerate(frames)))

    def test_known_area_and_stationary_motion(self):
        result = self.measure([self.vertices, self.vertices])
        self.assertAlmostEqual(result['first_frame_bbox_diagonal'], 2 ** .5)
        self.assertAlmostEqual(result['minimum_normalized_triangle_area'], .25)
        self.assertEqual(result['near_degenerate_face_frames'], 0)
        self.assertEqual(result['motion']['mean'], 0)

    def test_translation_peak_and_scale_invariance(self):
        frames = [self.vertices, self.vertices + [0, 0, .1],
                  self.vertices + [0, 0, .4], self.vertices + [0, 0, .4]]
        result = self.measure(frames)
        scaled = self.measure([v * 10 for v in frames])
        self.assertAlmostEqual(result['motion']['max'], .3 / 2 ** .5)
        self.assertAlmostEqual(result['motion']['mean'], .4 / (3 * 2 ** .5))
        self.assertAlmostEqual(result['motion']['max'], scaled['motion']['max'])
        self.assertAlmostEqual(result['minimum_normalized_triangle_area'],
                               scaled['minimum_normalized_triangle_area'])
        self.assertEqual(result['largest_transitions'][0]['from_frame'], 1)
        self.assertEqual(result['largest_transitions'][0]['to_frame'], 2)
        self.assertAlmostEqual(result['largest_transitions'][0]['pts'], 2 / 30)

    def test_collapsed_triangle_and_topology_diagnostics(self):
        # One duplicate (opposite winding), one repeated-index face,
        # and one unused vertex. A later frame collapses all three faces.
        vertices = np.vstack([self.vertices, [1, 1, 0]])
        faces = np.array([[0, 1, 2], [2, 1, 0], [0, 0, 1]])
        collapsed = vertices.copy()
        collapsed[2] = [.5, 0, 0]
        result = self.measure([vertices, collapsed], faces)
        self.assertEqual(result['duplicate_faces_ignoring_winding'], 1)
        self.assertEqual(result['repeated_index_faces'], 1)
        self.assertEqual(result['unreferenced_vertices'], 1)
        self.assertEqual(result['near_degenerate_face_frames'], 4)
        self.assertEqual(result['frames_with_near_degenerate_faces'], 2)
        self.assertEqual([f['near_degenerate_faces'] for f in result['area_by_frame']], [1, 3])
        self.assertEqual(result['area_by_frame'][1]['minimum_normalized_triangle_area'], 0)
        self.assertEqual(result['area_by_frame'][1]['frame'], 1)
        self.assertAlmostEqual(result['area_by_frame'][1]['pts'], 1 / 30)

    def test_one_frame_has_no_motion_summary(self):
        result = self.measure([self.vertices])
        self.assertEqual(result['motion'], dict(pairs=0, mean=None, p95=None, max=None))
        self.assertEqual(result['largest_transitions'], [])

    def test_invalid_geometry_and_clock_are_rejected(self):
        for frames, faces in [
            ([], self.faces),
            ([self.vertices * 0], self.faces),
            ([self.vertices * np.nan], self.faces),
            ([self.vertices], np.array([[0, 1, 3]])),
            ([self.vertices], np.array([[0., 1., 2.]])),
            ([self.vertices, self.vertices[:2]], self.faces),
        ]:
            with self.subTest(frames=len(frames), faces=faces.tolist()):
                with self.assertRaises(ValueError):
                    self.measure(frames, faces)
        with self.assertRaises(ValueError):
            measure_geometry(self.faces, iter([(0, self.vertices), (0, self.vertices)]))


if __name__ == '__main__':
    unittest.main()
