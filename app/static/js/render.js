// World: +X right, +Y forward, +Z up. Plane Euler convention is yaw about
// world Z, pitch about the yawed local right axis, then roll about plane normal.
const radians=value=>(+value||0)*Math.PI/180;
const add=(a,b)=>({x:a.x+b.x,y:a.y+b.y,z:a.z+b.z});
const scale=(v,n)=>({x:v.x*n,y:v.y*n,z:v.z*n});
const dot=(a,b)=>a.x*b.x+a.y*b.y+a.z*b.z;
const cross=(a,b)=>({x:a.y*b.z-a.z*b.y,y:a.z*b.x-a.x*b.z,z:a.x*b.y-a.y*b.x});
const rotate=(v,axis,angle)=>add(add(scale(v,Math.cos(angle)),scale(cross(axis,v),Math.sin(angle))),scale(axis,dot(axis,v)*(1-Math.cos(angle))));

export function projectPoint(p,camera,width,height){
  const pan=radians(camera.pan),tilt=-radians(camera.tilt),roll=-radians(camera.roll);
  let x=p.x-(camera.x??0),y=p.y-(camera.y??0),z=p.z-(camera.z??camera.height??1.7);
  [x,y]=[x*Math.cos(pan)-y*Math.sin(pan),x*Math.sin(pan)+y*Math.cos(pan)];
  [y,z]=[y*Math.cos(tilt)-z*Math.sin(tilt),y*Math.sin(tilt)+z*Math.cos(tilt)];
  [x,z]=[x*Math.cos(roll)-z*Math.sin(roll),x*Math.sin(roll)+z*Math.cos(roll)];
  if(y<=.05)return null;
  const fov=Math.max(5,Math.min(160,camera.fov||60))*Math.PI/180,focal=width/(2*Math.tan(fov/2));
  return{x:width/2+x/y*focal,y:height/2-z/y*focal,depth:y,scale:focal/y};
}

export function planeBasis(item){
  const yaw=radians(item.yaw_deg),pitch=radians(item.pitch_deg),roll=radians(item.roll_deg);
  let right={x:Math.cos(yaw),y:Math.sin(yaw),z:0},up={x:0,y:0,z:1};
  up=rotate(up,right,pitch);
  const normal=cross(right,up);
  right=rotate(right,normal,roll);up=rotate(up,normal,roll);
  return{right,up,normal};
}

export function planeWorldCorners(item,aspect=1){
  const center={x:+item.x||0,y:+item.y||0,z:+item.z||0},basis=planeBasis(item);
  const height=item.type==='text'?Math.max(.001,+item.world_height_m||1):Math.max(.001,+item.height_m||1);
  const width=item.type==='text'?height*Math.max(.01,aspect):Math.max(.001,+item.width_m||1);
  const r=scale(basis.right,width/2),u=scale(basis.up,height/2);
  return[add(add(center,scale(r,-1)),u),add(add(center,r),u),add(add(center,r),scale(u,-1)),add(add(center,scale(r,-1)),scale(u,-1))];
}

export function groupForItem(scene,itemId){return(scene.groups||[]).find(group=>(group.item_ids||[]).includes(itemId))||null;}
export function isItemEffectivelyVisible(item,scene){if(!item.visible)return false;const group=groupForItem(scene,item.id);return!group||group.visible!==false;}
export function projectionMode(camera,allowProvisional=false){if(!camera?.valid)return{allowed:false,mode:'tracking-invalid'};if(camera.world_valid)return{allowed:true,mode:'calibrated'};if(allowProvisional)return{allowed:true,mode:'provisional'};return{allowed:false,mode:'world-invalid'};}

function billboardQuad(item,p){
  const height=item.type==='image'?Math.max(.001,+item.height_m||1)*p.scale:Math.max(16,(+item.size||42)*p.scale*.08);
  const aspect=item.type==='image'?Math.max(.01,(+item.width_m||1)/(+item.height_m||1)):Math.max(.5,String(item.text||'').length*.58);
  const width=height*aspect;
  return[{x:p.x-width/2,y:p.y-height/2},{x:p.x+width/2,y:p.y-height/2},{x:p.x+width/2,y:p.y+height/2},{x:p.x-width/2,y:p.y+height/2}];
}

export function projectItemGeometry(item,camera,width,height,allowProvisional=false,aspect=1){
  if(!projectionMode(camera,allowProvisional).allowed)return null;
  if(item.type==='line'){
    const a=projectPoint({x:+item.x1,y:+item.y1,z:+item.z1},camera,width,height),b=projectPoint({x:+item.x2,y:+item.y2,z:+item.z2},camera,width,height);
    return a&&b?{type:'line',a,b}:null;
  }
  const p=projectPoint({x:+item.x,y:+item.y,z:+item.z},camera,width,height);
  if(!p)return null;
  if((item.type==='text'||item.type==='image')&&item.orientation_mode==='world'){
    const quad=planeWorldCorners(item,aspect).map(corner=>projectPoint(corner,camera,width,height));
    return quad.every(Boolean)?{type:'plane',kind:item.type,p,quad}:null;
  }
  if(item.type==='text'||item.type==='image')return{type:item.type,p,quad:billboardQuad(item,p)};
  return{type:item.type,p};
}

const triangleContains=(p,a,b,c)=>{const s=(p1,p2,p3)=>(p1.x-p3.x)*(p2.y-p3.y)-(p2.x-p3.x)*(p1.y-p3.y),d1=s(p,a,b),d2=s(p,b,c),d3=s(p,c,a),neg=d1<0||d2<0||d3<0,pos=d1>0||d2>0||d3>0;return!(neg&&pos);};
export function pointInProjectedQuad(point,geometry){const q=geometry?.quad;return!!q&&(triangleContains(point,q[0],q[1],q[2])||triangleContains(point,q[0],q[2],q[3]));}

const imageCache=new Map(),textCache=new Map();
function imageFor(item){if(!item.asset_id||typeof Image==='undefined')return null;if(!imageCache.has(item.asset_id)){const image=new Image();image.src=`/api/assets/${encodeURIComponent(item.asset_id)}`;imageCache.set(item.asset_id,image);}const image=imageCache.get(item.asset_id);return image.complete&&image.naturalWidth?image:null;}
function textTexture(item){
  if(typeof document==='undefined')return null;const key=[item.text,item.color,item.size].join('|');if(textCache.has(key))return textCache.get(key);
  const size=Math.max(16,Math.min(256,+item.size||42)),probe=document.createElement('canvas').getContext('2d');probe.font=`700 ${size}px system-ui, sans-serif`;const width=Math.max(8,Math.ceil(probe.measureText(item.text||' ').width+size*.35)),height=Math.ceil(size*1.35),canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const ctx=canvas.getContext('2d');ctx.font=`700 ${size}px system-ui, sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillStyle=item.color||'#fff';ctx.fillText(item.text||'',width/2,height/2);const result={image:canvas,aspect:width/height};textCache.set(key,result);return result;
}
function bilerp(q,u,v){return{x:(1-u)*(1-v)*q[0].x+u*(1-v)*q[1].x+u*v*q[2].x+(1-u)*v*q[3].x,y:(1-u)*(1-v)*q[0].y+u*(1-v)*q[1].y+u*v*q[2].y+(1-u)*v*q[3].y};}
function drawTriangle(ctx,image,src,dst){
  const [s0,s1,s2]=src,[d0,d1,d2]=dst,den=s0.x*(s1.y-s2.y)+s1.x*(s2.y-s0.y)+s2.x*(s0.y-s1.y);if(Math.abs(den)<1e-8)return;
  const a=(d0.x*(s1.y-s2.y)+d1.x*(s2.y-s0.y)+d2.x*(s0.y-s1.y))/den,b=(d0.y*(s1.y-s2.y)+d1.y*(s2.y-s0.y)+d2.y*(s0.y-s1.y))/den,c=(d0.x*(s2.x-s1.x)+d1.x*(s0.x-s2.x)+d2.x*(s1.x-s0.x))/den,d=(d0.y*(s2.x-s1.x)+d1.y*(s0.x-s2.x)+d2.y*(s1.x-s0.x))/den,e=(d0.x*(s1.x*s2.y-s2.x*s1.y)+d1.x*(s2.x*s0.y-s0.x*s2.y)+d2.x*(s0.x*s1.y-s1.x*s0.y))/den,f=(d0.y*(s1.x*s2.y-s2.x*s1.y)+d1.y*(s2.x*s0.y-s0.x*s2.y)+d2.y*(s0.x*s1.y-s1.x*s0.y))/den;
  ctx.save();ctx.beginPath();ctx.moveTo(d0.x,d0.y);ctx.lineTo(d1.x,d1.y);ctx.lineTo(d2.x,d2.y);ctx.closePath();ctx.clip();ctx.setTransform(a,b,c,d,e,f);ctx.drawImage(image,0,0);ctx.restore();
}
export function drawTexturedQuad(ctx,image,quad,steps=8){
  const sw=image.width||image.naturalWidth,sh=image.height||image.naturalHeight;if(!sw||!sh)return;
  for(let y=0;y<steps;y++)for(let x=0;x<steps;x++){const u0=x/steps,u1=(x+1)/steps,v0=y/steps,v1=(y+1)/steps,p00=bilerp(quad,u0,v0),p10=bilerp(quad,u1,v0),p11=bilerp(quad,u1,v1),p01=bilerp(quad,u0,v1),s00={x:u0*sw,y:v0*sh},s10={x:u1*sw,y:v0*sh},s11={x:u1*sw,y:v1*sh},s01={x:u0*sw,y:v1*sh};drawTriangle(ctx,image,[s00,s10,s11],[p00,p10,p11]);drawTriangle(ctx,image,[s00,s11,s01],[p00,p11,p01]);}
}

export function renderScene(canvas,state,options={}){
  const ctx=canvas.getContext('2d'),rect=canvas.getBoundingClientRect(),dpr=Math.min(globalThis.devicePixelRatio||1,2),w=Math.max(320,Math.round(rect.width*dpr)),h=Math.max(180,Math.round(rect.height*dpr));if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
  ctx.clearRect(0,0,w,h);if(!options.transparent){ctx.fillStyle=state.scene.background||'#00ff00';ctx.fillRect(0,0,w,h);}
  const camera=options.camera||state.render_camera||state.camera,projection=projectionMode(camera,options.allowProvisional===true);let projectedItems=0;
  if(projection.allowed)for(const item of state.scene.items||[]){if(!isItemEffectivelyVisible(item,state.scene))continue;const texture=item.type==='text'&&item.orientation_mode==='world'?textTexture(item):null,geometry=projectItemGeometry(item,camera,w,h,options.allowProvisional===true,texture?.aspect||1);if(!geometry)continue;
    if(item.type==='line'){ctx.strokeStyle=item.color||'#fff';ctx.lineWidth=(+item.width||4)*dpr;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(geometry.a.x,geometry.a.y);ctx.lineTo(geometry.b.x,geometry.b.y);ctx.stroke();projectedItems++;}
    else if(item.type==='text'&&item.orientation_mode==='world'){if(texture){drawTexturedQuad(ctx,texture.image,geometry.quad);projectedItems++;}}
    else if(item.type==='text'){const p=geometry.p,size=Math.max(8,Math.min(200,(+item.size||42)*p.scale*.08));ctx.fillStyle=item.color||'#fff';ctx.font=`700 ${size}px system-ui, sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(item.text||'',p.x,p.y);projectedItems++;}
    else if(item.type==='image'){const image=imageFor(item);if(image){drawTexturedQuad(ctx,image,geometry.quad,item.orientation_mode==='world'?8:1);projectedItems++;}}
    else if(item.type==='marker'){const p=geometry.p,radius=Math.max(5*dpr,Math.min(45*dpr,(+item.size||.5)*p.scale));ctx.strokeStyle=item.color||'#fff';ctx.fillStyle=item.color||'#fff';ctx.lineWidth=Math.max(2*dpr,radius*.18);ctx.beginPath();ctx.arc(p.x,p.y,radius,0,Math.PI*2);ctx.stroke();ctx.beginPath();ctx.moveTo(p.x-radius*1.45,p.y);ctx.lineTo(p.x+radius*1.45,p.y);ctx.moveTo(p.x,p.y-radius*1.45);ctx.lineTo(p.x,p.y+radius*1.45);ctx.stroke();if(item.show_label&&item.label){ctx.font=`700 ${Math.max(10*dpr,Math.min(30*dpr,radius*.8))}px system-ui, sans-serif`;ctx.textAlign='center';ctx.textBaseline='bottom';ctx.fillText(item.label,p.x,p.y-radius*1.7);}projectedItems++;}
  }
  if(options.crosshair){ctx.strokeStyle='rgba(255,255,255,.55)';ctx.lineWidth=dpr;ctx.beginPath();ctx.moveTo(w/2-15*dpr,h/2);ctx.lineTo(w/2+15*dpr,h/2);ctx.moveTo(w/2,h/2-15*dpr);ctx.lineTo(w/2,h/2+15*dpr);ctx.stroke();}
  return{projection,projectedItems};
}
