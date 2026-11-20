import math
import unittest
from app.vision_motion import VisionMotionAnalyzer, VisionMotionError

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


def context(pan=100, tilt=200, zoom=0, fov=56, ref='r1'):
    return {'tracking_valid': True,
            'references': {'pan': ref+'p', 'tilt': ref+'t', 'zoom': ref+'z'},
            'axes': {'pan': {'raw_count': pan}, 'tilt': {'raw_count': tilt},
                     'zoom': {'relative_count': zoom}},
            'camera': {'fov': fov},
            'mapping': {
                'pan': {'ppr': 600, 'quadrature_multiplier': 4, 'encoder_revs_per_camera_rev': 4.559715223},
                'tilt': {'ppr': 600, 'quadrature_multiplier': 4, 'encoder_revs_per_camera_rev': 1.2},
            }}


@unittest.skipUnless(cv2 is not None, 'OpenCV is optional on test host')
class MotionTests(unittest.TestCase):
    def pair(self, dx=9, dy=1):
        rng=np.random.default_rng(25)
        frame=rng.integers(0,256,(480,640), dtype=np.uint8)
        frame=cv2.GaussianBlur(frame,(3,3),.7)
        moved=cv2.warpAffine(frame,np.float32([[1,0,dx],[0,1,dy]]),(640,480))
        return [cv2.imencode('.png',f)[1].tobytes() for f in (frame,moved)]

    def test_reports_pixel_quantization_and_motion(self):
        a,b=self.pair(9,1)
        result=VisionMotionAnalyzer().analyse(a,b,context(),context(pan=109))
        self.assertTrue(result['review_only'])
        self.assertFalse(result['safe_to_apply'])
        self.assertEqual(result['dominant_axis'],'pan')
        self.assertAlmostEqual(result['motion_px']['x'],9,delta=.75)
        self.assertAlmostEqual(result['motion_px']['y'],1,delta=.75)
        self.assertGreater(result['inlier_fraction'],.8)
        self.assertAlmostEqual(result['axis_precision']['pan']['degrees_per_count'],
                               360/(600*4*4.559715223), delta=.00001)

    def test_reference_and_zoom_changes_are_rejected(self):
        a,b=self.pair()
        with self.assertRaisesRegex(VisionMotionError,'reference changed'):
            VisionMotionAnalyzer().analyse(a,b,context(),context(ref='r2'))
        with self.assertRaisesRegex(VisionMotionError,'Zoom moved'):
            VisionMotionAnalyzer().analyse(a,b,context(),context(zoom=1))

    def test_no_tracking_and_invalid_frame_rejected(self):
        a,b=self.pair()
        bad=context();bad['tracking_valid']=False
        with self.assertRaisesRegex(VisionMotionError,'valid referenced'):
            VisionMotionAnalyzer().analyse(a,b,bad,context())
        with self.assertRaisesRegex(VisionMotionError,'Images missing'):
            VisionMotionAnalyzer().analyse(b'x',b,context(),context(pan=110))


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(cv2 is not None, 'OpenCV is optional on test host')
class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_only_capture_is_review_only_and_never_changes_context(self):
        import tempfile
        from pathlib import Path
        from app.vision_calibration import VisionCalibrationService
        rng=np.random.default_rng(71)
        image=cv2.GaussianBlur(rng.integers(0,256,(480,640),dtype=np.uint8),(3,3),.7)
        shifted=cv2.warpAffine(image,np.float32([[1,0,8],[0,1,0]]),(640,480))
        images=[cv2.imencode('.png',value)[1].tobytes() for value in [image,shifted]]
        class FakeObs:
            async def screenshot(self,*args):
                return images.pop(0)
        with tempfile.TemporaryDirectory() as temp:
            service=VisionCalibrationService(Path(temp),obs_client=FakeObs())
            service.start()
            service.configure_obs({'host':'localhost','port':4455,'source':'Live Camera','password':''})
            seq=iter([context(),context(pan=108)])
            result=await service.capture_obs_motion(lambda: next(seq),interval_seconds=.01)
            self.assertEqual(result['dominant_axis'],'pan')
            self.assertFalse(result['safe_to_apply'])
            self.assertEqual(result['count_delta']['pan'],8)
            self.assertEqual(service.status()['completed'][0]['source'],'obs_motion')
