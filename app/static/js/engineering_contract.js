export const ENGINEERING_AXES = Object.freeze(['pan', 'tilt', 'zoom', 'focus']);

export const ENGINEERING_STATE_FIELDS = Object.freeze([
  'profiles', 'tracking', 'camera', 'render_camera', 'sync',
  'world_calibrations', 'engineering', 'clients',
]);

export const SOURCE_CHOICES = Object.freeze([
  Object.freeze({value:'simulator', label:'Simulator', implemented:true}),
  Object.freeze({value:'disabled', label:'Disabled', implemented:true}),
  Object.freeze({value:'quadrature_gpio', label:'Quadrature GPIO', implemented:true, axes:['pan', 'tilt']}),
  Object.freeze({value:'imu', label:'IMU (future — not implemented)', implemented:false}),
  Object.freeze({value:'external', label:'External (future — not implemented)', implemented:false}),
]);

export function sourceChoicesFor(axis) {
  return SOURCE_CHOICES.map(choice => ({
    ...choice,
    label: choice.axes && !choice.axes.includes(axis) ? `${choice.label} (Pan/Tilt only)` : choice.label,
    enabled: choice.implemented && (!choice.axes || choice.axes.includes(axis)),
  }));
}

export function validateEngineeringState(state) {
  if (!state || typeof state !== 'object') throw new Error('State payload is unavailable');
  for (const field of ENGINEERING_STATE_FIELDS) {
    if (!(field in state)) throw new Error(`State payload is missing ${field}`);
  }
  if (!state.profiles?.active?.axes) throw new Error('Active setup profile axes are unavailable');
  if (!state.tracking?.axes) throw new Error('Live tracking axes are unavailable');
  for (const axis of ENGINEERING_AXES) {
    if (!state.profiles.active.axes[axis]) throw new Error(`Setup profile is missing ${axis}`);
    if (!state.tracking.axes[axis]) throw new Error(`Tracking state is missing ${axis}`);
  }
  return state;
}
