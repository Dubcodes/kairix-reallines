import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

async function browserModule(path) {
  const source=await readFile(new URL(path,import.meta.url),'utf8');
  return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}

const {groupForItem,isItemEffectivelyVisible,projectPoint}=await browserModule('../app/static/js/render.js');
const {
  boundsForItems, distanceToSegment, fitTopDown, screenToWorldOnZPlane, snapValue,
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
console.log('editor math tests passed');
