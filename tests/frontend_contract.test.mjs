import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ENGINEERING_AXES,
  ENGINEERING_STATE_FIELDS,
  buildEncoderSourceConfig,
  sourceChoicesFor,
  validateEncoderDraft,
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

test('GPIO draft is validated and built as one coherent source_config update', () => {
  const valid = {chip:'/dev/gpiochip0', line_a:'17', line_b:'27', bias:'as_is'};
  assert.deepEqual(validateEncoderDraft(valid), {
    chip:'/dev/gpiochip0', line_a:17, line_b:27, bias:'as_is',
  });
  assert.deepEqual(buildEncoderSourceConfig(valid), {
    source_config:{chip:'/dev/gpiochip0', line_a:17, line_b:27, bias:'as_is'},
  });

  const invalid = [
    [{...valid, chip:' '}, /gpiochip device is required/],
    [{...valid, line_a:''}, /Line A is required/],
    [{...valid, line_b:''}, /Line B is required/],
    [{...valid, line_b:'17'}, /different lines/],
    [{...valid, line_a:'-1'}, /integer zero or greater/],
    [{...valid, line_a:'1.5'}, /integer zero or greater/],
    [{...valid, line_a:'GPIO17'}, /integer zero or greater/],
    [{...valid, bias:'automatic'}, /GPIO bias/],
  ];
  for (const [draft, message] of invalid) {
    assert.throws(() => buildEncoderSourceConfig(draft), message);
  }
});
