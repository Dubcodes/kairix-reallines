import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {
  clearDotLockSession,createDotLockSession,dotLockRay,dotLockReferencesMatch,
  dotLockWorldPoint,normalizedScreenPoint,projectDotLockSession,wedgeMarkerGeometry,
  shouldHidePredictedMarker,magnifierCrop,
} from '../app/static/js/dot_lock.js';
import {SUPPORTED_DIAGNOSTIC_PAGES,browserMetricsSummary,createBrowserMetrics,noteIncomingPose,noteRenderedFrame,poseIdentity} from '../app/static/js/browser_metrics.js';

async function browserModule(path) {
  const source=await readFile(new URL(path,import.meta.url),'utf8');
  return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}

const {groupForItem,isItemEffectivelyVisible,projectPoint}=await browserModule('../app/static/js/render.js');
const {
  boundsForItems, distanceToSegment, fitTopDown, itemDistanceDiagnostics, screenToWorldOnZPlane, snapValue,
  topDownToWorld, worldToTopDown,
}=await browserModule('../app/static/js/editor.js');

const camera={x:3,y:-2,z:1.9,pan:23,tilt:-12,roll:5,fov:68,height:1.9};
const width=1280,height=720;
for(const point of [{x:2.4,y:12,z:0},{x:-3,y:20,z:.4},{x:1,y:6,z:1.1}]) {
  const screen=projectPoint(point,camera,width,height);
  assert.ok(screen,'test point must be camera-visible');
  const restored=screenToWorldOnZPlane(screen,camera,width,height,point.z);
  assert.ok(restored,'projected ray must intersect the source Z plane');
  assert.ok(Math.abs(restored.x-point.x)<1e-9);
  assert.ok(Math.abs(restored.y-point.y)<1e-9);
  assert.equal(restored.z,point.z);
}

{
  const calibrated={x:0,y:0,z:0,world_valid:true};
  const marker=itemDistanceDiagnostics({type:'marker',x:3,y:4,z:12},calibrated);
  assert.equal(marker.distance_3d_m,13);
  assert.equal(marker.horizontal_m,5);
  assert.equal(marker.provisional,false);
  const line=itemDistanceDiagnostics({type:'line',x1:0,y1:3,z1:4,x2:6,y2:8,z2:0},calibrated);
  assert.equal(line.endpoints.a.distance_3d_m,5);
  assert.equal(line.endpoints.b.horizontal_m,10);
  assert.equal(itemDistanceDiagnostics({type:'marker',x:1,y:2,z:3},{x:0,y:0,z:0,world_valid:false}).provisional,true);
}

const centreCamera={x:3,y:2,z:1.8,pan:0,tilt:0,roll:0,fov:60,height:1.8};
const directlyAhead=projectPoint({x:3,y:12,z:1.8},centreCamera,1280,720);
assert.ok(Math.abs(directlyAhead.x-640)<1e-9);
assert.ok(Math.abs(directlyAhead.y-360)<1e-9);
const fixedPointAfterPan=projectPoint({x:3,y:12,z:1.8},{...centreCamera,pan:10},1280,720);
assert.ok(fixedPointAfterPan.x<640,'a fixed ahead point moves left when the camera pans right');
const rightRayAtCentre=projectPoint({x:13,y:12,z:1.8},{...centreCamera,pan:45},1280,720);
assert.ok(Math.abs(rightRayAtCentre.x-640)<1e-9);
assert.ok(Math.abs(rightRayAtCentre.y-360)<1e-9);
const upRayAtCentre=projectPoint({x:3,y:12,z:11.8},{...centreCamera,tilt:45},1280,720);
assert.ok(Math.abs(upRayAtCentre.x-640)<1e-9);
assert.ok(Math.abs(upRayAtCentre.y-360)<1e-9);

const view={centerX:2,centerY:8,scale:40};
const world={x:-1.25,y:14.5,z:0};
const screen=worldToTopDown(world,view,900,600);
const roundTrip=topDownToWorld(screen,view,900,600);
assert.ok(Math.abs(roundTrip.x-world.x)<1e-12);
assert.ok(Math.abs(roundTrip.y-world.y)<1e-12);
assert.equal(snapValue(1.26,.5),1.5);
assert.equal(snapValue(-1.26,.5),-1.5);
assert.equal(distanceToSegment({x:2,y:3},{x:0,y:0},{x:4,y:0}),3);

const fitted=fitTopDown([{type:'line',x1:-10,y1:0,x2:10,y2:20}],1000,600,50);
assert.equal(fitted.centerX,0);
assert.equal(fitted.centerY,10);
assert.ok(fitted.scale>0);

const marker={id:'marker',type:'marker',x:40,y:200,visible:true};
const markerFit=fitTopDown([marker],1000,600,50);
assert.equal(markerFit.centerX,20); // Fit All also includes the world/camera origin.
assert.equal(markerFit.centerY,100);
const bounds=boundsForItems([{type:'line',x1:-5,y1:2,x2:8,y2:4},marker]);
assert.deepEqual(bounds,{minX:-5,maxX:40,minY:2,maxY:200});

const scene={groups:[{id:'g',visible:false,item_ids:['marker']}],items:[marker]};
assert.equal(groupForItem(scene,'marker').id,'g');
assert.equal(isItemEffectivelyVisible(marker,scene),false);
scene.groups[0].visible=true;
assert.equal(isItemEffectivelyVisible(marker,scene),true);
marker.visible=false;
assert.equal(isItemEffectivelyVisible(marker,scene),false);

const dotCamera={x:0,y:0,z:1.7,pan:0,tilt:0,roll:0,fov:60,height:1.7,valid:true,world_valid:false};
const centreRay=dotLockRay({x:.5,y:.5},dotCamera,1280,720);
assert.ok(Math.abs(centreRay.direction.x)<1e-12);
assert.ok(Math.abs(centreRay.direction.y-1)<1e-12);
assert.ok(Math.abs(centreRay.direction.z)<1e-12);
const upperRightRay=dotLockRay({x:.75,y:.25},dotCamera,1280,720);
assert.ok(upperRightRay.direction.x>0);
assert.ok(upperRightRay.direction.z>0);
const centreWorld=dotLockWorldPoint({x:.5,y:.5},dotCamera,10,1280,720);
assert.deepEqual(centreWorld,{x:0,y:10,z:1.7});
const dotInputs=[{id:1,x:.5,y:.5},{id:2,x:.35,y:.4},{id:3,x:.65,y:.6},{id:4,x:.8,y:.25}];
const dotSceneBefore=structuredClone(scene);
const dotSession=createDotLockSession({camera:dotCamera,points:dotInputs,targetDistance:10,referenceIds:{pan:'P1',tilt:'T1',zoom:'Z1'},width:1280,height:720});
const fixedWorld=structuredClone(dotSession.points.map(point=>point.world));
const referenceProjection=projectDotLockSession(dotSession,dotCamera,1280,720);
for(let index=0;index<dotInputs.length;index++){
  assert.ok(Math.abs(referenceProjection[index].predicted.x-dotInputs[index].x)<1e-12);
  assert.ok(Math.abs(referenceProjection[index].predicted.y-dotInputs[index].y)<1e-12);
}
const pannedProjection=projectDotLockSession(dotSession,{...dotCamera,pan:8},1280,720);
const tiltedProjection=projectDotLockSession(dotSession,{...dotCamera,tilt:5},1280,720);
const zoomedProjection=projectDotLockSession(dotSession,{...dotCamera,fov:30},1280,720);
assert.notDeepEqual(pannedProjection.map(point=>point.predicted),referenceProjection.map(point=>point.predicted));
assert.notDeepEqual(tiltedProjection.map(point=>point.predicted),referenceProjection.map(point=>point.predicted));
assert.notDeepEqual(zoomedProjection.map(point=>point.predicted),referenceProjection.map(point=>point.predicted));
assert.deepEqual(dotSession.points.map(point=>point.world),fixedWorld,'provisional world points stay fixed while the camera moves');
const restoredProjection=projectDotLockSession(dotSession,dotCamera,1280,720);
assert.deepEqual(restoredProjection.map(point=>point.predicted),referenceProjection.map(point=>point.predicted));
const resizedProjection=projectDotLockSession(dotSession,dotCamera,640,360);
for(let index=0;index<dotInputs.length;index++){
  assert.ok(Math.abs(resizedProjection[index].predicted.x-dotInputs[index].x)<1e-12);
  assert.ok(Math.abs(resizedProjection[index].predicted.y-dotInputs[index].y)<1e-12);
}
assert.deepEqual(normalizedScreenPoint({x:.25,y:.75},800,400),{x:200,y:300});
assert.equal(dotLockReferencesMatch(dotSession,{pan:'P1',tilt:'T1',zoom:'Z1'}),true);
assert.equal(dotLockReferencesMatch(dotSession,{pan:'P2',tilt:'T1',zoom:'Z1'}),false);
assert.equal(clearDotLockSession(),null);
assert.deepEqual(scene,dotSceneBefore,'Dot Lock does not alter scene/layout objects');
console.log('editor math tests passed');

{
  const metrics=createBrowserMetrics();
  noteIncomingPose(metrics,0,'pose-1');noteIncomingPose(metrics,20,'pose-1');noteIncomingPose(metrics,40,'pose-2');
  noteRenderedFrame(metrics,{now:40,renderDurationMs:2,poseKey:'pose-1',poseValid:true,canvasWidth:1920,canvasHeight:1080,dpr:1});
  noteRenderedFrame(metrics,{now:60,renderDurationMs:4,poseKey:'pose-1',poseValid:true,canvasWidth:1920,canvasHeight:1080,dpr:1});
  noteRenderedFrame(metrics,{now:80,renderDurationMs:3,poseKey:'pose-2',poseValid:false,canvasWidth:1920,canvasHeight:1080,dpr:1});
  const summary=browserMetricsSummary(metrics,80);
  assert.equal(summary.pose_interarrival_p95_ms,20);
  assert.equal(summary.frame_interval_p95_ms,20);
  assert.equal(summary.render_p95_ms,3);
  assert.equal(summary.held_poses,1);
  assert.equal(summary.duplicate_pose_messages,1);
  assert.equal(summary.invalid_tracking_frames,1);
  assert.equal(summary.canvas_width,1920);
  assert.equal(summary.animation_frame_hz_5s,50);
  assert.equal(summary.incoming_pose_hz_5s,50);
  assert.equal(summary.presentation_timing_available,false);
  assert.equal(SUPPORTED_DIAGNOSTIC_PAGES.has('calibration'),false);
  assert.equal(poseIdentity({render_camera:{render_timestamp:12.5,interpolation_status:'OK',valid:true}}),'12.5:OK:valid');
  assert.equal(poseIdentity({render_camera:{pan:1}}),null);
}

{
  const four=wedgeMarkerGeometry(100,100,20,4,4),three=wedgeMarkerGeometry(100,100,20,3,4);
  assert.equal(four.length,4);assert.equal(three.length,3);
  for(const wedge of [...four,...three])assert.ok(wedge.every(point=>Math.hypot(point.x-100,point.y-100)>=3.999),'wedge geometry preserves the clear centre gap');
  assert.equal(shouldHidePredictedMarker({x:.5,y:.5},{x:.501,y:.5},1920,1080,4,1),true);
  assert.equal(shouldHidePredictedMarker({x:.5,y:.5},{x:.51,y:.5},1920,1080,4,1),false);
  const crop=magnifierCrop({x:.5,y:.5},1920,1080,6);
  assert.deepEqual(crop,{x:800,y:450,width:320,height:180});
}

{
  const metrics=createBrowserMetrics();
  noteRenderedFrame(metrics,{now:0,renderDurationMs:9,poseKey:'old',poseValid:true});
  noteRenderedFrame(metrics,{now:10000,renderDurationMs:1,poseKey:'a',poseValid:true});
  noteRenderedFrame(metrics,{now:10020,renderDurationMs:1,poseKey:'b',poseValid:true});
  noteRenderedFrame(metrics,{now:10040,renderDurationMs:1,poseKey:'c',poseValid:true});
  const summary=browserMetricsSummary(metrics,10040);
  assert.equal(summary.animation_frame_hz_5s,50,'old inactivity does not dilute the current 5-second rate');
  assert.equal(summary.render_mean_ms_5s,1);
  assert.equal(summary.missed_frame_deadlines_5s,0);
}
