const trackers=new Map();
const limited=(values,value,limit=240)=>{values.push(value);if(values.length>limit)values.shift();};
const percentile=(values,p=.95)=>{if(!values.length)return null;const sorted=[...values].sort((a,b)=>a-b);return sorted[Math.min(sorted.length-1,Math.floor((sorted.length-1)*p))];};

export function poseIdentity(state){
  const raw=state?.render_camera?.render_timestamp;
  if(raw===null||raw===undefined)return null;
  const timestamp=Number(raw);
  return Number.isFinite(timestamp)?`${timestamp}:${state?.render_camera?.interpolation_status||''}:${state?.render_camera?.valid===true?'valid':'invalid'}`:null;
}

export function createBrowserMetrics(){return{started:null,lastPose:null,lastFrame:null,lastPoseKey:null,lastIncomingPoseKey:null,poseCount:0,frameCount:0,heldPoses:0,missingPoses:0,stalePoses:0,invalidTrackingFrames:0,duplicatePoseMessages:0,poseIntervals:[],frameIntervals:[],renderDurations:[],canvasWidth:0,canvasHeight:0,dpr:1,lastPublish:0,lastRemotePublish:0};}
const tracker=page=>{if(!trackers.has(page))trackers.set(page,createBrowserMetrics());return trackers.get(page);};

export function noteIncomingPose(metrics,now,poseKey=null){
  if(metrics.started===null)metrics.started=now;
  if(metrics.lastPose!==null)limited(metrics.poseIntervals,now-metrics.lastPose);
  if(poseKey!==null&&poseKey===metrics.lastIncomingPoseKey)metrics.duplicatePoseMessages+=1;
  metrics.lastIncomingPoseKey=poseKey;metrics.lastPose=now;metrics.poseCount+=1;
}

export function noteRenderedFrame(metrics,{now,renderDurationMs,poseKey=null,poseValid=null,canvasWidth=0,canvasHeight=0,dpr=1}){
  if(metrics.started===null)metrics.started=now;
  if(metrics.lastFrame!==null)limited(metrics.frameIntervals,now-metrics.lastFrame);
  metrics.lastFrame=now;metrics.frameCount+=1;limited(metrics.renderDurations,Math.max(0,renderDurationMs));
  if(poseKey===null||poseKey===undefined)metrics.missingPoses+=1;
  else if(poseKey===metrics.lastPoseKey)metrics.heldPoses+=1;
  metrics.lastPoseKey=poseKey;
  if(metrics.lastPose===null||now-metrics.lastPose>100)metrics.stalePoses+=1;
  if(poseValid===false)metrics.invalidTrackingFrames+=1;
  metrics.canvasWidth=canvasWidth;metrics.canvasHeight=canvasHeight;metrics.dpr=dpr;
}

export function browserMetricsSummary(metrics,now=metrics.lastFrame??metrics.lastPose??0){
  const elapsed=Math.max(.001,(now-(metrics.started??now))/1000);
  const mean=values=>values.length?values.reduce((sum,value)=>sum+value,0)/values.length:null;
  return{
    incoming_pose_hz:metrics.poseCount/elapsed,
    pose_interarrival_mean_ms:mean(metrics.poseIntervals),pose_interarrival_p95_ms:percentile(metrics.poseIntervals),
    animation_frame_hz:metrics.frameCount/elapsed,frame_interval_p95_ms:percentile(metrics.frameIntervals),
    render_mean_ms:mean(metrics.renderDurations),render_p95_ms:percentile(metrics.renderDurations),
    held_poses:metrics.heldPoses,missing_poses:metrics.missingPoses,stale_poses:metrics.stalePoses,
    invalid_tracking_frames:metrics.invalidTrackingFrames,duplicate_pose_messages:metrics.duplicatePoseMessages,
    canvas_width:metrics.canvasWidth,canvas_height:metrics.canvasHeight,device_pixel_ratio:metrics.dpr,
    measured_at_browser_ms:now,
  };
}

export function recordIncomingPose(page,poseKey=null,now=performance.now()){
  const metrics=tracker(page);noteIncomingPose(metrics,now,poseKey);publish(page,metrics,now);
}

export function recordRenderedFrame(page,values){
  const metrics=tracker(page);noteRenderedFrame(metrics,values);publish(page,metrics,values.now);
}

function clientIdentity(){
  const key='kairix.browser_metrics.client_id';
  try{
    let value=localStorage.getItem(key);
    if(!value){value=(globalThis.crypto?.randomUUID?.()||`client-${Math.random().toString(36).slice(2)}`).replaceAll(':','-');localStorage.setItem(key,value);}
    return value;
  }catch(_error){return'ephemeral-client';}
}

function publish(page,metrics,now){
  if(now-metrics.lastPublish<1000)return;metrics.lastPublish=now;
  const summary=browserMetricsSummary(metrics,now);
  try{localStorage.setItem(`kairix.browser_metrics.${page}`,JSON.stringify(summary));}catch(_error){}
  if(now-metrics.lastRemotePublish<5000)return;metrics.lastRemotePublish=now;
  const label=new URLSearchParams(location.search).get('metrics_name')||'';
  fetch('/api/browser-metrics',{method:'POST',headers:{'Content-Type':'application/json'},keepalive:true,body:JSON.stringify({client_id:clientIdentity(),page,label,metrics:summary})}).catch(()=>{});
}
