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
import {projectItemGeometry, projectionMode} from '../app/static/js/render.js';

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

test('projection policy separates invalid, provisional, and calibrated cameras', () => {
  const invalid = {valid:false, world_valid:false};
  assert.deepEqual(projectionMode(invalid, true), {allowed:false, mode:'tracking-invalid'});

  const provisional = {valid:true, world_valid:false, x:0, y:0, z:1.7, pan:0, tilt:0, roll:0, fov:60};
  assert.deepEqual(projectionMode(provisional, false), {allowed:false, mode:'world-invalid'});
  assert.deepEqual(projectionMode(provisional, true), {allowed:true, mode:'provisional'});
  assert.equal(provisional.world_valid, false, 'presentation policy must not mutate world validity');

  const calibrated = {...provisional, world_valid:true};
  assert.deepEqual(projectionMode(calibrated, false), {allowed:true, mode:'calibrated'});
});

test('a default new Line projects provisionally and exposes selection geometry', () => {
  const camera = {valid:true, world_valid:false, x:0, y:0, z:1.7, pan:0, tilt:-8, roll:0, fov:60};
  const line = {type:'line', x1:-2, y1:10, z1:0, x2:2, y2:10, z2:0};
  assert.equal(projectItemGeometry(line, camera, 1280, 720, false), null, 'strict GFX blocks world-invalid projection');
  const geometry = projectItemGeometry(line, camera, 1280, 720, true);
  assert.ok(geometry, 'Control/provisional GFX can project a forward line');
  assert.equal(geometry.type, 'line');
  assert.ok(geometry.a.x < 640 && geometry.b.x > 640);
  assert.equal(projectItemGeometry(line, {...camera, valid:false}, 1280, 720, true), null, 'override never bypasses invalid tracking');
});
