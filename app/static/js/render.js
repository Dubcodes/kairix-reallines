// Lightweight world -> camera projection for the first development build.
// World: +X right, +Y forward, +Z up. Positive pan turns right;
// positive tilt turns up. camera.pan/tilt are world-facing angles.
export function projectPoint(p, camera, width, height) {
  const pan = (camera.pan || 0) * Math.PI / 180;
  const tilt = -(camera.tilt || 0) * Math.PI / 180;
  const roll = -(camera.roll || 0) * Math.PI / 180;
  let x = p.x - (camera.x ?? 0);
  let y = p.y - (camera.y ?? 0);
  let z = p.z - (camera.z ?? camera.height ?? 1.7);

  // Rotate world opposite camera pan about Z.
  const xp = x * Math.cos(pan) - y * Math.sin(pan);
  const yp = x * Math.sin(pan) + y * Math.cos(pan);
  x = xp; y = yp;

  // Rotate opposite camera tilt about camera X/right axis.
  const yt = y * Math.cos(tilt) - z * Math.sin(tilt);
  const zt = y * Math.sin(tilt) + z * Math.cos(tilt);
  y = yt; z = zt;

  // Fixed camera roll rotates the camera's image/right-up axes.
  const xr = x * Math.cos(roll) - z * Math.sin(roll);
  const zr = x * Math.sin(roll) + z * Math.cos(roll);
  x = xr; z = zr;

  if (y <= 0.05) return null;
  const fov = Math.max(5, Math.min(160, camera.fov || 60)) * Math.PI / 180;
  const focal = width / (2 * Math.tan(fov / 2));
  return {
    x: width / 2 + (x / y) * focal,
    y: height / 2 - (z / y) * focal,
    depth: y,
    scale: focal / y,
  };
}

export function groupForItem(scene, itemId) {
  return (scene.groups || []).find(group => (group.item_ids || []).includes(itemId)) || null;
}

export function isItemEffectivelyVisible(item, scene) {
  if (!item.visible) return false;
  const group = groupForItem(scene, item.id);
  return !group || group.visible !== false;
}

export function renderScene(canvas, state, options={}) {
  const ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(320, Math.round(rect.width * dpr));
  const h = Math.max(180, Math.round(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width=w; canvas.height=h; }

  ctx.clearRect(0,0,w,h);
  ctx.fillStyle = options.transparent ? 'rgba(0,0,0,0)' : (state.scene.background || '#00ff00');
  ctx.fillRect(0,0,w,h);

  const camera = options.camera || state.render_camera || state.camera;
  if (camera?.valid && camera?.world_valid) for (const item of state.scene.items || []) {
    if (!isItemEffectivelyVisible(item, state.scene)) continue;
    if (item.type === 'line') {
      const a = projectPoint({x:+item.x1,y:+item.y1,z:+item.z1},camera,w,h);
      const b = projectPoint({x:+item.x2,y:+item.y2,z:+item.z2},camera,w,h);
      if (!a || !b) continue;
      ctx.strokeStyle = item.color || '#fff';
      ctx.lineWidth = (+item.width || 4) * dpr;
      ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke();
    }
    if (item.type === 'text') {
      const p = projectPoint({x:+item.x,y:+item.y,z:+item.z},camera,w,h);
      if (!p) continue;
      const size = Math.max(8, Math.min(200, (+item.size || 42) * p.scale * 0.08));
      ctx.fillStyle = item.color || '#fff';
      ctx.font = `700 ${size}px system-ui, sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(item.text || '', p.x, p.y);
    }
    if (item.type === 'marker') {
      const p = projectPoint({x:+item.x,y:+item.y,z:+item.z},camera,w,h);
      if (!p) continue;
      const radius = Math.max(5*dpr, Math.min(45*dpr, (+item.size || .5) * p.scale));
      ctx.strokeStyle = item.color || '#fff'; ctx.fillStyle = item.color || '#fff';
      ctx.lineWidth = Math.max(2*dpr, radius*.18);
      ctx.beginPath(); ctx.arc(p.x,p.y,radius,0,Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(p.x-radius*1.45,p.y); ctx.lineTo(p.x+radius*1.45,p.y); ctx.moveTo(p.x,p.y-radius*1.45); ctx.lineTo(p.x,p.y+radius*1.45); ctx.stroke();
      if (item.show_label && item.label) {
        const labelSize = Math.max(10*dpr, Math.min(30*dpr, radius*.8));
        ctx.font = `700 ${labelSize}px system-ui, sans-serif`; ctx.textAlign='center';ctx.textBaseline='bottom';
        ctx.fillText(item.label,p.x,p.y-radius*1.7);
      }
    }
  }

  if (options.crosshair) {
    ctx.strokeStyle='rgba(255,255,255,.55)'; ctx.lineWidth=dpr;
    ctx.beginPath(); ctx.moveTo(w/2-15*dpr,h/2); ctx.lineTo(w/2+15*dpr,h/2); ctx.moveTo(w/2,h/2-15*dpr); ctx.lineTo(w/2,h/2+15*dpr); ctx.stroke();
  }
}
