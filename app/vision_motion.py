"""Review-only optical flow vs Pico count displacement, on two camera-only stills.

This is an offline calibration diagnostic. No tracking state, encoder references,
profile or accepted world-calibration data is ever modified by this module.
Optical flow of background is not an absolute surveyed angle measurement.
"""
from __future__ import annotations

import math
from typing import Any


class VisionMotionError(ValueError):
    pass


def _camera_focal_px(width: int, horizontal_fov_deg: float) -> float:
    if not (5.0 <= horizontal_fov_deg <= 160.0):
        raise VisionMotionError("Horizontal FOV must be between 5° and 160°")
    return width / (2 * math.tan(math.radians(horizontal_fov_deg / 2)))


def _ref(context: dict, axis: str):
    return (context.get('references') or {}).get(axis)


def _raw(context: dict, axis: str):
    item = (context.get('axes') or {}).get(axis) or {}
    return item.get('relative_count') if axis == 'zoom' else item.get('raw_count')


def _scale_from_context(ctx: dict, axis: str, width: int, focal: float) -> dict[str, Any]:
    mapping = (ctx.get('mapping') or {}).get(axis) or {}
    try:
        ppr = float(mapping['ppr'])
        multiplier = float(mapping['quadrature_multiplier'])
        ratio = float(mapping['encoder_revs_per_camera_rev'])
        if not all(math.isfinite(x) and x > 0 for x in (ppr, multiplier, ratio)):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise VisionMotionError(f"Missing valid {axis} mapping to assess pixel/count precision") from exc
    degrees = 360.0 / (ppr * multiplier * ratio)
    return {'degrees_per_count': round(degrees, 7),
            'centre_pixels_per_count': round(math.radians(degrees) * focal, 4),
            'ppr': ppr, 'quadrature_multiplier': multiplier, 'gear_ratio': ratio}


class VisionMotionAnalyzer:
    """Conservative Shi-Tomasi + bidirectional LK background displacement estimate.

    Only propose a ratio *diagnostic*, never a calibration model change. Mixed
    camera motion, moving scenery, zoom or reference changes reject the test.
    """
    def analyse(self, before: bytes, after: bytes, start: dict, end: dict) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise VisionMotionError('Install opencv-python-headless to use vision motion diagnostics') from exc
        if not start.get('tracking_valid') or not end.get('tracking_valid'):
            raise VisionMotionError('Both captures require valid referenced tracking')
        for axis in ('pan', 'tilt', 'zoom'):
            if not _ref(start, axis) or _ref(start, axis) != _ref(end, axis):
                raise VisionMotionError(f'{axis} reference changed or was not set; discard pair')
        try:
            changes = {axis: int(_raw(end, axis)) - int(_raw(start, axis)) for axis in ('pan', 'tilt', 'zoom')}
        except (ValueError, TypeError) as exc:
            raise VisionMotionError('Missing encoder counts in image capture context') from exc
        if changes['zoom'] != 0:
            raise VisionMotionError('Zoom moved during test; hold Zoom fixed for Pan/Tilt motion measurement')
        try:
            fov1 = float((start.get('camera') or {})['fov'])
            fov2 = float((end.get('camera') or {})['fov'])
        except (ValueError, TypeError, KeyError) as exc:
            raise VisionMotionError('Both captures must have a known horizontal FOV') from exc
        if abs(fov1-fov2) > .02:
            raise VisionMotionError('FOV changed between captures; use a locked lens setting')
        a = cv2.imdecode(np.frombuffer(before, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        b = cv2.imdecode(np.frombuffer(after, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if a is None or b is None or a.shape != b.shape:
            raise VisionMotionError('Images missing, invalid or not the same dimensions')
        h, w = a.shape
        if w < 320 or h < 180:
            raise VisionMotionError('Images too small; use a camera-only image at least 320×180')
        focal = _camera_focal_px(w, fov1)
        scales = {axis: _scale_from_context(start, axis, w, focal) for axis in ('pan', 'tilt')}
        # Work on 1280-wide maximum for predictable CPU/RAM load. All reported
        # shifts are converted back to full-source pixels (subpixel precision).
        factor = min(1.0, 1280.0/w)
        if factor < 1:
            a_small = cv2.resize(a, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
            b_small = cv2.resize(b, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
        else:
            a_small, b_small = a, b
        pts = cv2.goodFeaturesToTrack(a_small, maxCorners=500, qualityLevel=.015,
                                      minDistance=9, blockSize=5)
        if pts is None or len(pts) < 30:
            raise VisionMotionError('Not enough static scene texture (need at least 30 visual corners)')
        lk = dict(winSize=(21, 21), maxLevel=3,
                  criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, .01))
        nxt, good, _ = cv2.calcOpticalFlowPyrLK(a_small, b_small, pts, None, **lk)
        if nxt is None:
            raise VisionMotionError('Image motion could not be tracked')
        rev, back_good, _ = cv2.calcOpticalFlowPyrLK(b_small, a_small, nxt, None, **lk)
        if rev is None:
            raise VisionMotionError('Reverse optical-flow validation failed')
        mask = (good.ravel() == 1) & (back_good.ravel() == 1) & (
            np.linalg.norm(pts.reshape(-1, 2)-rev.reshape(-1, 2), axis=1) < 1.5)
        old = pts.reshape(-1, 2)[mask]
        new = nxt.reshape(-1, 2)[mask]
        if len(old) < 24:
            raise VisionMotionError('Insufficient reliable tracked features after reverse check')
        affine, inliers = cv2.estimateAffinePartial2D(old, new, method=cv2.RANSAC,
                                                       ransacReprojThreshold=2.5,
                                                       maxIters=1500, confidence=.99)
        if affine is None or inliers is None:
            raise VisionMotionError('Unable to establish coherent background motion')
        mask_in = inliers.ravel().astype(bool)
        inlier_ratio = float(mask_in.mean())
        if mask_in.sum() < 20 or inlier_ratio < .60:
            raise VisionMotionError('Low scene-motion agreement: moving subjects or parallax dominate')
        inlier_flow = (new-old)[mask_in] / factor
        dx, dy = (float(np.median(inlier_flow[:, i])) for i in (0, 1))
        residual = np.linalg.norm(inlier_flow-np.array([dx, dy]), axis=1)
        p95 = float(np.percentile(residual, 95))
        if p95 > max(3.0, .010*max(w, h)):
            raise VisionMotionError('Motion is not a consistent translation; use distant fixed landmarks and a small movement')
        pan_px = abs(changes['pan']) * scales['pan']['centre_pixels_per_count']
        tilt_px = abs(changes['tilt']) * scales['tilt']['centre_pixels_per_count']
        # Do not confuse background camera-relative flow with ground truth
        # image lock. Need one dominant axis to interpret an observed ratio.
        dominant = None
        if pan_px >= 3*max(tilt_px, 1) and pan_px >= 3:
            dominant = 'pan'
        elif tilt_px >= 3*max(pan_px, 1) and tilt_px >= 3:
            dominant = 'tilt'
        observed = abs(dx) if dominant == 'pan' else abs(dy) if dominant == 'tilt' else None
        predicted = pan_px if dominant == 'pan' else tilt_px if dominant == 'tilt' else None
        ratio = (observed / predicted) if predicted is not None and predicted >= 3 else None
        warnings = []
        if not dominant:
            warnings.append('Pan/Tilt axes moved together or by too few counts; isolate one axis for meaningful comparison')
        if inlier_ratio < .75:
            warnings.append('Scene motion has modest agreement; use static distant objects')
        if any(v['centre_pixels_per_count'] >= 1 for v in scales.values()):
            warnings.append('Encoder quantization may exceed one source pixel near frame centre; larger output resolution alone will not increase angular precision')
        if ratio is not None and (ratio < .75 or ratio > 1.25):
            warnings.append('Observed motion differs from current pinhole/FOV/gear model; investigate FOV, target distance, gearing and lens distortion before changing settings')
        return {'review_only': True, 'safe_to_apply': False,
                'source_width': w, 'source_height': h,
                'tracked_features': int(len(old)), 'inlier_features': int(mask_in.sum()),
                'inlier_fraction': round(inlier_ratio, 3),
                'motion_px': {'x': round(dx, 3), 'y': round(dy, 3)},
                'residual_p95_px': round(p95, 3), 'count_delta': changes,
                'horizontal_fov_deg': round(fov1, 3),
                'axis_precision': scales, 'dominant_axis': dominant,
                'expected_displacement_px': round(predicted, 3) if predicted is not None else None,
                'observed_displacement_px': round(observed, 3) if observed is not None else None,
                'observed_to_expected_ratio': round(ratio, 3) if ratio is not None else None,
                'warnings': warnings,
                'limitations': 'Image translation is an approximation: parallax, distortion, subject motion, depth, FOV errors and OBS scaling can affect results. Does not establish absolute encoder accuracy.'}
