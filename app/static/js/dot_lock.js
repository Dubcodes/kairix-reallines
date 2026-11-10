import {cameraRayForScreen} from './editor.js';
import {projectPoint} from './render.js';

const add=(a,b)=>({x:a.x+b.x,y:a.y+b.y,z:a.z+b.z});
const scale=(value,factor)=>({x:value.x*factor,y:value.y*factor,z:value.z*factor});
const dot=(a,b)=>a.x*b.x+a.y*b.y+a.z*b.z;
const normalize=value=>{const length=Math.hypot(value.x,value.y,value.z);return scale(value,1/length);};

export function normalizedScreenPoint(point,width,height){
  return{x:Number(point.x)*width,y:Number(point.y)*height};
}

export function dotLockRay(point,camera,width=1280,height=720){
  return cameraRayForScreen(normalizedScreenPoint(point,width,height),camera,width,height);
}

export function dotLockWorldPoint(point,camera,targetDistance,width=1280,height=720){
  const distance=Number(targetDistance);
  if(!Number.isFinite(distance)||distance<=0)throw new Error('Target distance must be greater than zero');
  const optical=normalize(cameraRayForScreen({x:width/2,y:height/2},camera,width,height).direction);
  const planePoint=add({x:camera.x??0,y:camera.y??0,z:camera.z??camera.height??1.7},scale(optical,distance));
  const ray=dotLockRay(point,camera,width,height),denominator=dot(ray.direction,optical);
  if(Math.abs(denominator)<1e-9)throw new Error('Dot ray does not intersect the target plane');
  const travel=dot({x:planePoint.x-ray.origin.x,y:planePoint.y-ray.origin.y,z:planePoint.z-ray.origin.z},optical)/denominator;
  if(travel<=0)throw new Error('Dot target plane is behind the camera');
  return add(ray.origin,scale(ray.direction,travel));
}

export function createDotLockSession({camera,points,targetDistance,referenceIds,width=1280,height=720}){
  if(!camera)throw new Error('Render camera is unavailable');
  if(!Array.isArray(points)||points.length<1)throw new Error('Place at least one dot');
  return{
    target_distance_m:Number(targetDistance),
    reference_camera:{...camera},
    reference_ids:{...referenceIds},
    points:points.map((point,index)=>({
      id:point.id??index+1,
      normalized:{x:Number(point.x),y:Number(point.y)},
      world:dotLockWorldPoint(point,camera,targetDistance,width,height),
    })),
  };
}

export function projectDotLockSession(session,camera,width=1280,height=720){
  return session.points.map(point=>{
    const projected=projectPoint(point.world,camera,width,height);
    return{...point,predicted:projected?{x:projected.x/width,y:projected.y/height}:null};
  });
}

export function dotLockReferencesMatch(session,currentReferenceIds){
  if(!session)return false;
  return ['pan','tilt','zoom'].every(axis=>(session.reference_ids?.[axis]??null)===(currentReferenceIds?.[axis]??null));
}

export function clearDotLockSession(){return null;}
