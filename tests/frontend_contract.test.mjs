import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ENGINEERING_AXES,
  ENGINEERING_STATE_FIELDS,
  sourceChoicesFor,
  validateEngineeringState,
} from '../app/static/js/engineering_contract.js';

test('Engineering source choices expose only implemented paths as usable', () => {
  for (const axis of ENGINEERING_AXES) {
    const choices = Object.fromEntries(sourceChoicesFor(axis).map(choice => [choice.value, choice]));
    assert.equal(choices.simulator.enabled, true);
    assert.equal(choices.disabled.enabled, true);
    assert.equal(choices.imu.enabled, false);
    assert.equal(choices.external.enabled, false);
    assert.match(choices.imu.label, /not implemented/);
    assert.match(choices.external.label, /not implemented/);
    assert.equal(choices.quadrature_gpio.enabled, axis === 'pan' || axis === 'tilt');
  }
});

test('Engineering state contract accepts the required page payload', () => {
  const state = Object.fromEntries(ENGINEERING_STATE_FIELDS.map(field => [field, {}]));
  state.profiles = {active:{axes:{}}};
  state.tracking = {axes:{}};
  for (const axis of ENGINEERING_AXES) {
    state.profiles.active.axes[axis] = {source:'simulator'};
    state.tracking.axes[axis] = {source:'simulator'};
  }
  assert.equal(validateEngineeringState(state), state);
  const broken = {...state};
  delete broken.sync;
  assert.throws(() => validateEngineeringState(broken), /missing sync/);
});
