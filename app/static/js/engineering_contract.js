export const ENGINEERING_AXES = Object.freeze(['pan', 'tilt', 'zoom', 'focus']);

export const ENGINEERING_STATE_FIELDS = Object.freeze([
  'profiles', 'tracking', 'camera', 'render_camera', 'sync',
  'world_calibrations', 'engineering', 'clients',
]);

export const SOURCE_CHOICES = Object.freeze([
  Object.freeze({value:'simulator', label:'Simulator', implemented:true}),
  Object.freeze({value:'disabled', label:'Disabled', implemented:true}),
  Object.freeze({value:'quadrature_gpio', label:'Quadrature GPIO', implemented:true, axes:['pan', 'tilt']}),
  Object.freeze({value:'linux_rotary', label:'Linux Rotary Encoder', implemented:true, axes:['pan', 'tilt']}),
  Object.freeze({value:'imu', label:'IMU (future — not implemented)', implemented:false}),
  Object.freeze({value:'external', label:'External (future — not implemented)', implemented:false}),
]);

export const GPIO_BIASES = Object.freeze(['as_is', 'pull_up', 'pull_down']);

export function sourceChoicesFor(axis) {
  return SOURCE_CHOICES.map(choice => ({
    ...choice,
    label: choice.axes && !choice.axes.includes(axis) ? `${choice.label} (Pan/Tilt only)` : choice.label,
    enabled: choice.implemented && (!choice.axes || choice.axes.includes(axis)),
  }));
}

export function validateEncoderDraft(draft) {
  const chip = String(draft?.chip ?? '').trim();
  if (!chip) throw new Error('gpiochip device is required');

  const parseLine = (value, label) => {
    if (value === '' || value === null || value === undefined) {
      throw new Error(`${label} is required`);
    }
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) {
      throw new Error(`${label} must be an integer zero or greater`);
    }
    return parsed;
  };

  const lineA = parseLine(draft.line_a, 'Line A');
  const lineB = parseLine(draft.line_b, 'Line B');
  if (lineA === lineB) throw new Error('GPIO A and B must use different lines');

  const bias = String(draft.bias ?? '');
  if (!GPIO_BIASES.includes(bias)) {
    throw new Error('GPIO bias must be as_is, pull_up or pull_down');
  }
  return {chip, line_a:lineA, line_b:lineB, bias};
}

export function buildEncoderSourceConfig(draft) {
  return {source_config:validateEncoderDraft(draft)};
}

export function buildSourceUpdate(source) {
  return source === 'linux_rotary'
    ? {source, mapping:{quadrature_multiplier:4}}
    : {source};
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
