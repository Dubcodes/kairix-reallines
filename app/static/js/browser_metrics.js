const trackers=new Map();
export const SUPPORTED_DIAGNOSTIC_PAGES=new Set(['gfx','preview','control']);
const limited=(values,value,limit=240)=>{values.push(value);if(values.length>limit)values.shift();};
const percentile=(values,p=.95)=>{if(!values.length)return null;const sorted=[...values].sort((a,b)=>a-b);return sorted[Math.min(sorted.length-1,Math.floor((sorted.length-1)*p))];};
const timed=(values,now,value)=>{values.push([now,value]);while(values.length&&now-values[0][0]>30000)values.shift();};
const windowValues=(values,now,windowMs)=>values.filter(([stamp,value])=>now-stamp<=windowMs&&stamp-value>=now-windowMs).map(([,value])=>value);
const rollingRate=(times,now,windowMs)=>{const recent=times.filter(stamp=>now-stamp<=windowMs);if(recent.length<2)return recent.length?0:null;const span=Math.max(1,recent.at(-1)-recent[0]);return(recent.length-1)/(span/1000);};

export function poseIdentity(state){
  const raw=state?.render_camera?.render_timestamp;
  if(raw===null||raw===undefined)return null;
  const timestamp=Number(raw);
  return Number.isFinite(timestamp)?`${timestamp}:${state?.render_camera?.interpolation_status||''}:${state?.render_camera?.valid===true?'valid':'invalid'}`:null;
}

export function createBrowserMetrics(){return{started:null,lastPose:null,lastFrame:null,lastPoseKey:null,lastIncomingPoseKey:null,poseCount:0,frameCount:0,heldPoses:0,missingPoses:0,stalePoses:0,invalidTrackingFrames:0,duplicatePoseMessages:0,poseIntervals:[],frameIntervals:[],renderDurations:[],poseTimes:[],frameTimes:[],timedPoseIntervals:[],timedFrameIntervals:[],timedRenderDurations:[],canvasWidth:0,canvasHeight:0,dpr:1,lastPublish:0,lastRemotePublish:0,transportMode:'connecting',outputQueueQueued:0,outputQueueCoalesced:0,outputQueueOverflows:0};}
const tracker=page=>{if(!trackers.has(page))trackers.set(page,createBrowserMetrics());return trackers.get(page);};

export function noteIncomingPose(metrics,now,poseKey=null,transport=null){
  if(metrics.started===null)metrics.started=now;
  if(metrics.lastPose!==null){const interval=now-metrics.lastPose;limited(metrics.poseIntervals,interval);timed(metrics.timedPoseIntervals,now,interval);}
  if(poseKey!==null&&poseKey===metrics.lastIncomingPoseKey)metrics.duplicatePoseMessages+=1;
  metrics.lastIncomingPoseKey=poseKey;metrics.lastPose=now;metrics.poseCount+=1;metrics.poseTimes.push(now);while(metrics.poseTimes.length&&now-metrics.poseTimes[0]>30000)metrics.poseTimes.shift();
  if(transport){metrics.outputQueueQueued=Number(transport.queued_messages)||0;metrics.outputQueueCoalesced=Number(transport.coalesced_poses)||0;metrics.outputQueueOverflows=Number(transport.critical_overflows)||0;}
}

export function noteRenderedFrame(metrics,{now,renderDurationMs,poseKey=null,poseValid=null,canvasWidth=0,canvasHeight=0,dpr=1}){
  if(metrics.started===null)metrics.started=now;
  if(metrics.lastFrame!==null){const interval=now-metrics.lastFrame;limited(metrics.frameIntervals,interval);timed(metrics.timedFrameIntervals,now,interval);}
  const duration=Math.max(0,renderDurationMs);metrics.lastFrame=now;metrics.frameCount+=1;metrics.frameTimes.push(now);while(metrics.frameTimes.length&&now-metrics.frameTimes[0]>30000)metrics.frameTimes.shift();limited(metrics.renderDurations,duration);timed(metrics.timedRenderDurations,now,duration);
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
  const frame5=windowValues(metrics.timedFrameIntervals,now,5000),frame30=windowValues(metrics.timedFrameIntervals,now,30000),pose5=windowValues(metrics.timedPoseIntervals,now,5000),render5=windowValues(metrics.timedRenderDurations,now,5000),targetInterval=20;
  return{
    incoming_pose_hz:metrics.poseCount/elapsed,
    incoming_pose_hz_5s:rollingRate(metrics.poseTimes,now,5000),incoming_pose_hz_30s:rollingRate(metrics.poseTimes,now,30000),
    pose_interarrival_mean_ms:mean(metrics.poseIntervals),pose_interarrival_p95_ms:percentile(metrics.poseIntervals),
    animation_frame_hz:metrics.frameCount/elapsed,frame_interval_p95_ms:percentile(metrics.frameIntervals),
    animation_frame_hz_5s:rollingRate(metrics.frameTimes,now,5000),animation_frame_hz_30s:rollingRate(metrics.frameTimes,now,30000),
    frame_interval_mean_ms_5s:mean(frame5),frame_interval_p50_ms_5s:percentile(frame5,.5),frame_interval_p95_ms_5s:percentile(frame5),frame_interval_p99_ms_5s:percentile(frame5,.99),
    missed_frame_deadlines_5s:frame5.reduce((sum,value)=>sum+Math.max(0,Math.floor(value/targetInterval)-1),0),missed_frame_deadlines_30s:frame30.reduce((sum,value)=>sum+Math.max(0,Math.floor(value/targetInterval)-1),0),
    pose_interarrival_p95_ms_5s:percentile(pose5),
    render_mean_ms:mean(metrics.renderDurations),render_p95_ms:percentile(metrics.renderDurations),
    render_mean_ms_5s:mean(render5),render_p95_ms_5s:percentile(render5),
    held_poses:metrics.heldPoses,missing_poses:metrics.missingPoses,stale_poses:metrics.stalePoses,
    invalid_tracking_frames:metrics.invalidTrackingFrames,duplicate_pose_messages:metrics.duplicatePoseMessages,
    canvas_width:metrics.canvasWidth,canvas_height:metrics.canvasHeight,device_pixel_ratio:metrics.dpr,
    visibility_state:globalThis.document?.visibilityState||'unavailable',transport_mode:metrics.transportMode,
    output_queue_queued:metrics.outputQueueQueued,output_queue_coalesced:metrics.outputQueueCoalesced,output_queue_overflows:metrics.outputQueueOverflows,
    last_pose_identity:metrics.lastIncomingPoseKey,last_rendered_pose_identity:metrics.lastPoseKey,
    pose_age_ms:null,presentation_timing_available:false,
    measured_at_browser_ms:now,
  };
}

export function recordIncomingPose(page,poseKey=null,now=performance.now(),transport=null){
  if(!SUPPORTED_DIAGNOSTIC_PAGES.has(page))return;
  const metrics=tracker(page);noteIncomingPose(metrics,now,poseKey,transport);publish(page,metrics,now);
}

export function recordRenderedFrame(page,values){
  if(!SUPPORTED_DIAGNOSTIC_PAGES.has(page))return;
  const metrics=tracker(page);noteRenderedFrame(metrics,values);publish(page,metrics,values.now);
}

const browserInstanceIdentity=(globalThis.crypto?.randomUUID?.()||`client-${Math.random().toString(36).slice(2)}`).replaceAll(':','-');
function clientIdentity(){return browserInstanceIdentity;}

export function setBrowserTransport(page,mode){if(SUPPORTED_DIAGNOSTIC_PAGES.has(page))tracker(page).transportMode=String(mode||'unknown').slice(0,32);}

function publish(page,metrics,now){
  if(!SUPPORTED_DIAGNOSTIC_PAGES.has(page))return;
  if(now-metrics.lastPublish<1000)return;metrics.lastPublish=now;
  const summary=browserMetricsSummary(metrics,now);
  try{localStorage.setItem(`kairix.browser_metrics.${page}.${clientIdentity()}`,JSON.stringify(summary));localStorage.setItem(`kairix.browser_metrics.${page}`,JSON.stringify(summary));}catch(_error){}
  if(now-metrics.lastRemotePublish<5000)return;metrics.lastRemotePublish=now;
  const label=new URLSearchParams(location.search).get('metrics_name')||'';
  fetch('/api/browser-metrics',{method:'POST',headers:{'Content-Type':'application/json'},keepalive:true,body:JSON.stringify({client_id:clientIdentity(),page,label,metrics:summary})}).catch(()=>{});
}
