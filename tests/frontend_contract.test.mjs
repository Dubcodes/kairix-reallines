import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ENGINEERING_AXES,
  ENGINEERING_STATE_FIELDS,
  buildEncoderSourceConfig,
  buildSourceUpdate,
  sourceChoicesFor,
  validateEncoderDraft,
  validateEngineeringState,
} from '../app/static/js/engineering_contract.js';
import {planeWorldCorners, pointInProjectedQuad, projectItemGeometry, projectionMode} from '../app/static/js/render.js';
import {calculateMovementCalibration} from '../app/static/js/calibration_contract.js';

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
    assert.equal(choices.linux_rotary.enabled, axis === 'pan' || axis === 'tilt');
  }
});

test('Linux rotary source selection applies source and fixed x4 atomically', () => {
  assert.deepEqual(buildSourceUpdate('linux_rotary'), {
    source:'linux_rotary',
    mapping:{quadrature_multiplier:4},
  });
  assert.deepEqual(buildSourceUpdate('quadrature_gpio'), {source:'quadrature_gpio'});
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

test('floor and vertical world-plane presets produce deterministic world corners', () => {
  const floor={type:'image',x:2,y:8,z:.2,width_m:4,height_m:2,yaw_deg:0,pitch_deg:-90,roll_deg:0};
  const floorCorners=planeWorldCorners(floor);
  assert.ok(floorCorners.every(point=>Math.abs(point.z-.2)<1e-9), 'floor corners keep constant Z');
  const vertical=planeWorldCorners({...floor,pitch_deg:0});
  assert.ok(vertical.every(point=>Math.abs(point.y-8)<1e-9), 'vertical plane has constant Y at zero yaw');
  assert.deepEqual(planeWorldCorners(floor), floorCorners, 'plane transform is deterministic');
});

test('world planes perspective-scale while billboards remain screen-facing', () => {
  const camera={valid:true,world_valid:true,x:0,y:0,z:1.7,pan:12,tilt:-3,roll:0,fov:60};
  const billboard={type:'image',x:2,y:12,z:1.7,width_m:2,height_m:1,orientation_mode:'billboard'};
  const bg=projectItemGeometry(billboard,camera,1280,720,false);
  assert.equal(bg.quad[0].y,bg.quad[1].y);
  assert.equal(bg.quad[1].x,bg.quad[2].x);
  const near={...billboard,orientation_mode:'world',yaw_deg:20,pitch_deg:0,roll_deg:0};
  const far={...near,y:24};
  const ng=projectItemGeometry(near,camera,1280,720,false),fg=projectItemGeometry(far,camera,1280,720,false);
  const width=g=>Math.hypot(g.quad[1].x-g.quad[0].x,g.quad[1].y-g.quad[0].y);
  assert.ok(width(ng)>width(fg));
  assert.equal(pointInProjectedQuad(ng.p,ng),true);
  assert.equal(pointInProjectedQuad({x:-100,y:-100},ng),false);
});

test('camera pan changes floor-text projection without mutating its world transform', () => {
  const item={type:'text',text:'FINISH',x:0,y:15,z:0,world_height_m:1,orientation_mode:'world',yaw_deg:0,pitch_deg:-90,roll_deg:0};
  const before=structuredClone(item),camera={valid:true,world_valid:true,x:0,y:0,z:2,pan:0,tilt:-10,roll:0,fov:60};
  const first=projectItemGeometry(item,camera,1280,720,false,3.2),second=projectItemGeometry(item,{...camera,pan:10},1280,720,false,3.2);
  assert.ok(first&&second);
  assert.notDeepEqual(first.quad,second.quad);
  assert.deepEqual(item,before);
});

test('known-angle movement calibration computes gearing and direction', () => {
  const exact=calculateMovementCalibration({start_count:0,end_count:600,actual_angle:90,ppr:600,multiplier:4});
  assert.equal(exact.encoder_revs_per_camera_rev,1);
  const forward=calculateMovementCalibration({start_count:100,end_count:1300,actual_angle:90,ppr:600,multiplier:4});
  assert.equal(forward.encoder_turns,.5);
  assert.equal(forward.camera_fraction,.25);
  assert.equal(forward.encoder_revs_per_camera_rev,2);
  assert.equal(forward.direction,1);
  assert.equal(calculateMovementCalibration({start_count:1300,end_count:100,actual_angle:90,ppr:600,multiplier:4}).direction,-1);
  assert.throws(()=>calculateMovementCalibration({start_count:1,end_count:1,actual_angle:90,ppr:600,multiplier:4}),/Move/);
  assert.throws(()=>calculateMovementCalibration({start_count:1,end_count:2,actual_angle:0,ppr:600,multiplier:4}),/non-zero/);
  assert.throws(()=>calculateMovementCalibration({start_count:null,end_count:2,actual_angle:90,ppr:600,multiplier:4}),/required/);
});
