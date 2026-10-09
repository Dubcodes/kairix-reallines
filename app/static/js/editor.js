// Pure coordinate and hit-testing helpers used by the Control editor.
// World remains +X right, +Y forward, +Z up.

export function cameraRayForScreen(screen, camera, width, height) {
  const fov = Math.max(5, Math.min(160, camera.fov || 60)) * Math.PI / 180;
  const focal = width / (2 * Math.tan(fov / 2));
  const cameraX = (screen.x - width / 2) / focal;
  const cameraZ = (height / 2 - screen.y) / focal;
  const tilt = -(camera.tilt || 0) * Math.PI / 180;
  const pan = (camera.pan || 0) * Math.PI / 180;
  const roll = (camera.roll || 0) * Math.PI / 180;

  // Undo projectPoint's fixed roll.
  const unrolledX = Math.cos(roll) * cameraX - Math.sin(roll) * cameraZ;
  const unrolledZ = Math.sin(roll) * cameraX + Math.cos(roll) * cameraZ;

  // Undo projectPoint's tilt, then its pan.
  const panY = Math.cos(tilt) + Math.sin(tilt) * unrolledZ;
  const dz = -Math.sin(tilt) + Math.cos(tilt) * unrolledZ;
  return {
    origin: {x: camera.x ?? 0, y: camera.y ?? 0, z: camera.z ?? camera.height ?? 1.7},
    direction: {
      x: Math.cos(pan) * unrolledX + Math.sin(pan) * panY,
      y: -Math.sin(pan) * unrolledX + Math.cos(pan) * panY,
      z: dz,
    },
  };
}

export function screenToWorldOnZPlane(screen, camera, width, height, planeZ) {
  const ray = cameraRayForScreen(screen, camera, width, height);
  if (Math.abs(ray.direction.z) < 1e-9) return null;
  const distance = (planeZ - ray.origin.z) / ray.direction.z;
  if (distance <= 0) return null;
  return {
    x: ray.origin.x + ray.direction.x * distance,
    y: ray.origin.y + ray.direction.y * distance,
    z: planeZ,
  };
}

export function worldToTopDown(point, view, width, height) {
  return {
    x: width / 2 + (point.x - view.centerX) * view.scale,
    y: height / 2 - (point.y - view.centerY) * view.scale,
  };
}

export function topDownToWorld(screen, view, width, height, z=0) {
  return {
    x: view.centerX + (screen.x - width / 2) / view.scale,
    y: view.centerY - (screen.y - height / 2) / view.scale,
    z,
  };
}

export function snapValue(value, increment) {
  return increment > 0 ? Math.round(value / increment) * increment : value;
}

export function distanceToSegment(point, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length2 = dx * dx + dy * dy;
  const t = length2 ? Math.max(0, Math.min(1, ((point.x-a.x)*dx + (point.y-a.y)*dy) / length2)) : 0;
  return Math.hypot(point.x - (a.x + t*dx), point.y - (a.y + t*dy));
}

export function itemWorldPoints(item) {
  if (item.type === 'line') return [{x:+item.x1,y:+item.y1},{x:+item.x2,y:+item.y2}];
  if (item.type === 'text' || item.type === 'marker') return [{x:+item.x,y:+item.y}];
  return [];
}

export function boundsForItems(items) {
  const points = (items || []).flatMap(itemWorldPoints);
  if (!points.length) return null;
  return {
    minX:Math.min(...points.map(p=>p.x)), maxX:Math.max(...points.map(p=>p.x)),
    minY:Math.min(...points.map(p=>p.y)), maxY:Math.max(...points.map(p=>p.y)),
  };
}

export function fitTopDown(items, width, height, padding=60) {
  const points = (items || []).flatMap(itemWorldPoints);
  points.push({x:0,y:0});
  const minX = Math.min(...points.map(p=>p.x)), maxX = Math.max(...points.map(p=>p.x));
  const minY = Math.min(...points.map(p=>p.y)), maxY = Math.max(...points.map(p=>p.y));
  const spanX = Math.max(4, maxX-minX), spanY = Math.max(4, maxY-minY);
  return {
    centerX: (minX+maxX)/2,
    centerY: (minY+maxY)/2,
    scale: Math.max(0.25, Math.min(500, Math.min((width-2*padding)/spanX, (height-2*padding)/spanY))),
  };
}
