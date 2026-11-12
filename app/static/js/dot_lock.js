import {cameraRayForScreen} from './editor.js';
import {projectPoint} from './render.js';

export const withoutObservation=(observations,observationId)=>(observations||[]).filter(obs=>obs.id!==observationId);
export const replaceObservation=(observations,updated)=>(observations||[]).map(obs=>
  obs.id===updated.id?{...obs,points:structuredClone(updated.points)}:obs
);

const add=(a,b)=>({x:a.x+b.x,y:a.y+b.y,z:a.z+b.z});
const scale=(value,factor)=>({x:value.x*factor,y:value.y*factor,z:value.z*factor});
const dot=(a,b)=>a.x*b.x+a.y*b.y+a.z*b.z;
const normalize=value=>{const length=Math.hypot(value.x,value.y,value.z);return scale(value,1/length);};

export function nudgeDotPointPx(point,key,{shiftKey=false,ctrlKey=false,metaKey=false}={}){
  const amount=(ctrlKey||metaKey)?0.25:shiftKey?10:1;
  const directions={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
  const direction=directions[key];if(!direction)return null;
  const clamp=v=>Math.max(-0.25,Math.min(1.25,v));
  return{x:clamp(Number(point.x)+direction[0]*amount/1920),y:clamp(Number(point.y)+direction[1]*amount/1080)};
}

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
    points:points.map((point,index)=>(
      {
        id:point.id??`M${index+1}`,
        normalized:{x:Number(point.x),y:Number(point.y)},
        world:dotLockWorldPoint(point,camera,targetDistance,width,height),
      }
    )),
  };
}

export function projectDotLockSession(session,camera,width=1280,height=720){
  return session.points.map(point=>{
    const projected=projectPoint(point.world,camera,width,height);
    return{...point,predicted:projected?{x:projected.x/width,y:projected.y/height}:null};
  });
}

export function projectModelPoints(modelPoints,camera,width=1280,height=720){
  return Object.entries(modelPoints||{}).map(([id,world])=>{
    const projected=projectPoint(world,camera,width,height);
    return{id,world,predicted:projected?{x:projected.x/width,y:projected.y/height}:null};
  });
}

export function modelPointsFromObservation({camera,points,targetDistance,width=1280,height=720}){
  const session=createDotLockSession({camera,points,targetDistance,referenceIds:{},width,height});
  return Object.fromEntries(session.points.map(point=>[String(point.id),point.world]));
}

export function rectangleTemplatePoints(edgeMidpoints=false){
  const points=[
    {id:'M1',x:.34,y:.34},{id:'M2',x:.66,y:.34},{id:'M3',x:.66,y:.66},{id:'M4',x:.34,y:.66},
  ];
  if(edgeMidpoints)points.push(
    {id:'M5',x:.50,y:.34},{id:'M6',x:.66,y:.50},{id:'M7',x:.50,y:.66},{id:'M8',x:.34,y:.50},
  );
  return points;
}

export function dotLockReferencesMatch(session,currentReferenceIds){
  if(!session)return false;
  return ['pan','tilt','zoom'].every(axis=>(session.reference_ids?.[axis]??null)===(currentReferenceIds?.[axis]??null));
}

export function clearDotLockSession(){return null;}

export function hitTestDot(points,point,width,height,radius=20){
  let best=null,bestDistance=Infinity;
  for(const candidate of points||[]){
    const p=candidate.observed||candidate.predicted||candidate.normalized||candidate;
    if(!p)continue;
    const distance=Math.hypot((Number(p.x)-point.x)*width,(Number(p.y)-point.y)*height);
    if(distance<=radius&&distance<bestDistance){best=candidate;bestDistance=distance;}
  }
  return best;
}

export function drawDotCalibrationOverlay(ctx,width,height,{predicted=[],observed={},selected=null,showConnections=false,markers=[],markerScale=1,showLabels=true,showVectors=true}={}){
  const dpr=Math.min(globalThis.devicePixelRatio||1,2),scale=Math.max(.75,Math.min(4,Number(markerScale)||1));
  const predictedMap=new Map((predicted||[]).map(point=>[String(point.id),point.predicted]));
  const ids=markers.length?markers.map(marker=>String(marker.id)):[...new Set([...predictedMap.keys(),...Object.keys(observed||{})])];
  const observedMap=observed||{},pathIds=['M1','M2','M3','M4'];
  const label=(id,x,y,fill)=>{
    if(!showLabels)return;
    const fontSize=12*dpr*scale,pad=4*dpr*scale,text=String(id);
    ctx.save();ctx.font=`800 ${fontSize}px system-ui`;const metrics=ctx.measureText(text),w=metrics.width+pad*2,h=fontSize+pad*1.5;
    ctx.fillStyle='rgba(3,10,16,.82)';ctx.fillRect(x+12*dpr*scale,y-h-7*dpr*scale,w,h);
    ctx.fillStyle=fill;ctx.fillText(text,x+12*dpr*scale+pad,y-9*dpr*scale);ctx.restore();
  };
  if(showConnections&&pathIds.every(id=>observedMap[id]||predictedMap.get(id))){
    const drawPath=(source,style,dash=[])=>{ctx.save();ctx.strokeStyle=style;ctx.lineWidth=2*dpr*scale;ctx.setLineDash(dash.map(v=>v*dpr*scale));ctx.beginPath();pathIds.forEach((id,index)=>{const p=source(id);if(!p)return;const x=p.x*width,y=p.y*height;if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});const first=source(pathIds[0]);if(first)ctx.lineTo(first.x*width,first.y*height);ctx.stroke();ctx.restore();};
    drawPath(id=>predictedMap.get(id),'rgba(87,184,255,.78)',[6,5]);drawPath(id=>observedMap[id],'rgba(255,206,103,.96)');
  }
  for(const id of ids){
    const predictedPoint=predictedMap.get(id);
    if(predictedPoint){
      const x=predictedPoint.x*width,y=predictedPoint.y*height,r=12*dpr*scale;
      ctx.save();ctx.strokeStyle='#57b8ff';ctx.lineWidth=2.5*dpr*scale;ctx.beginPath();ctx.moveTo(x-r,y);ctx.lineTo(x+r,y);ctx.moveTo(x,y-r);ctx.lineTo(x,y+r);ctx.stroke();ctx.fillStyle='#57b8ff';ctx.beginPath();ctx.arc(x,y,2.5*dpr*scale,0,Math.PI*2);ctx.fill();label(id,x,y,'#7fcbff');ctx.restore();
    }
    const observedPoint=observedMap[id];
    if(observedPoint){
      const x=observedPoint.x*width,y=observedPoint.y*height,r=10*dpr*scale,stroke=String(selected)===id?'#fff7c9':'#ffce67';
      ctx.save();ctx.strokeStyle=stroke;ctx.fillStyle='rgba(255,206,103,.20)';ctx.lineWidth=3*dpr*scale;ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.beginPath();ctx.moveTo(x-r*1.45,y);ctx.lineTo(x+r*1.45,y);ctx.moveTo(x,y-r*1.45);ctx.lineTo(x,y+r*1.45);ctx.stroke();ctx.fillStyle=stroke;ctx.beginPath();ctx.arc(x,y,3*dpr*scale,0,Math.PI*2);ctx.fill();label(id,x,y,stroke);ctx.restore();
      if(predictedPoint&&showVectors){ctx.save();ctx.strokeStyle='rgba(255,95,111,.95)';ctx.lineWidth=2*dpr*scale;ctx.setLineDash([5*dpr*scale,4*dpr*scale]);ctx.beginPath();ctx.moveTo(predictedPoint.x*width,predictedPoint.y*height);ctx.lineTo(x,y);ctx.stroke();ctx.setLineDash([]);const angle=Math.atan2(y-predictedPoint.y*height,x-predictedPoint.x*width),ah=7*dpr*scale;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-ah*Math.cos(angle-.55),y-ah*Math.sin(angle-.55));ctx.moveTo(x,y);ctx.lineTo(x-ah*Math.cos(angle+.55),y-ah*Math.sin(angle+.55));ctx.stroke();ctx.restore();}
    }
  }
}

export function rmsOverlayError(predicted=[],observed={},width=1920,height=1080){
  const map=new Map((predicted||[]).filter(point=>point.predicted).map(point=>[String(point.id),point.predicted]));
  const errors=[];
  for(const [id,point] of Object.entries(observed||{})){
    const expected=map.get(String(id));if(!expected)continue;
    errors.push(Math.hypot((point.x-expected.x)*width,(point.y-expected.y)*height));
  }
  return errors.length?Math.sqrt(errors.reduce((sum,value)=>sum+value*value,0)/errors.length):null;
}

export function drawGfxCalibrationOverlay(canvas,state){
  const overlay=state?.calibration_overlay;if(!overlay?.enabled)return false;
  const dotData=state?.world_calibrations?.active?.dot_calibration,target=(dotData?.targets||[]).find(item=>item.id===overlay.target_id)||null;
  if(!target)return false;
  const ctx=canvas.getContext('2d'),width=canvas.width,height=canvas.height;
  const predicted=projectModelPoints(target.model_points||{},state.render_camera||state.camera,width,height);
  drawDotCalibrationOverlay(ctx,width,height,{
    predicted,observed:overlay.editing?(overlay.draft_points||{}):(overlay.reference_points||{}),selected:overlay.selected_marker,
    showConnections:target.type==='rectangle',markers:target.markers||[],markerScale:overlay.marker_scale??2,
    showLabels:overlay.show_labels!==false,showVectors:overlay.show_vectors!==false,
  });
  const dpr=Math.min(globalThis.devicePixelRatio||1,2),text='CALIBRATION OVERLAY — NOT FOR AIR';
  ctx.save();ctx.font=`800 ${14*dpr}px system-ui`;const metrics=ctx.measureText(text),pad=10*dpr;
  ctx.fillStyle='rgba(112,27,35,.92)';ctx.fillRect(14*dpr,height-48*dpr,metrics.width+pad*2,34*dpr);
  ctx.fillStyle='#ffe1e4';ctx.fillText(text,14*dpr+pad,height-25*dpr);ctx.restore();
  return true;
}

