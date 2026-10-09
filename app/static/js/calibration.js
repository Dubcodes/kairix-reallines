import {api,connectState,logFrontend} from './common.js?v=world1';

const byId=id=>document.getElementById(id);
const targets=['top_left','top_right','bottom_right','bottom_left'];
const targetLabel=value=>value.replaceAll('_',' ').toUpperCase();
let state=null,applying=false;

function format(value,digits=3){return Number.isFinite(+value)?(+value).toFixed(digits):'—';}
function error(message=''){byId('worldError').textContent=message;}
async function safe(action){try{error();return await action();}catch(reason){error(String(reason));logFrontend('calibration','world_calibration_ui_error',{error:String(reason)});return null;}}
function axisCard(axis){const runtime=state.tracking.axes[axis],mapping=state.profiles.active.axes[axis].mapping;return`<article class="axis-card"><div class="axis-heading"><h3>${axis.toUpperCase()}</h3><span class="axis-health health-${runtime.health.toLowerCase().replaceAll('_','-')}">${runtime.health}</span></div><div class="kv"><div>Source</div><div>${runtime.source}</div><div>Raw</div><div>${format(runtime.raw)}</div><div>Mapped</div><div>${runtime.value===null?'—':`${format(runtime.value)}°`}</div><div>Direction</div><div>${mapping.direction_learned?`${mapping.direction>0?'+1':'-1'} learned`:`${mapping.direction>0?'+1':'-1'} default`}</div><div>Valid</div><div>${runtime.valid?'YES':'NO'}</div></div></article>`;}
function setInput(selector,value){const input=document.querySelector(selector);if(input&&input!==document.activeElement)input.value=value;}

function renderCalibration(){
  const collection=state.world_calibrations,calibration=collection.active,solution=calibration.solution;
  byId('activeProfile').textContent=state.profiles.active.name;
  byId('profileMatch').textContent=collection.profile_match?'Setup profile matches this calibration.':'WARNING: this calibration belongs to a different setup profile.';
  byId('profileMatch').className=collection.profile_match?'small':'form-error';
  const select=byId('worldCalibrationSelect');
  if(select!==document.activeElement)select.replaceChildren(...collection.items.map(item=>{const option=document.createElement('option');option.value=item.id;option.textContent=item.name;option.selected=item.id===collection.active_id;return option;}));
  if(byId('worldCalibrationName')!==document.activeElement)byId('worldCalibrationName').value=calibration.name;
  for(const axis of ['x','y','z']){setInput(`[data-target-center="${axis}"]`,calibration.target.center[axis]);setInput(`[data-camera-hint="${axis}"]`,calibration.camera_hint[axis]);}
  for(const key of ['width','height','yaw'])setInput(`[data-target="${key}"]`,calibration.target[key]);
  for(const key of ['pan_offset','tilt_offset'])setInput(`[data-orientation-hint="${key}"]`,calibration.orientation_hint[key]);
  setInput('#worldFov',calibration.horizontal_fov);
  setInput('#worldRoll',calibration.fixed_roll);
  byId('status').textContent=collection.profile_match?calibration.status:'PROFILE MISMATCH';
  byId('status').className=calibration.valid&&collection.profile_match?'status-ok':'status-bad';
  byId('targetSteps').replaceChildren(...targets.map((target,index)=>{const button=document.createElement('button');button.className=`target-step${target===calibration.current_target?' active':''}${calibration.observations[target]?' marked':''}`;button.textContent=`${index+1}. ${targetLabel(target)} — ${calibration.observations[target]?'MARKED':'NOT MARKED'}`;button.onclick=()=>safe(()=>api('/api/world-calibration/target',{method:'POST',body:JSON.stringify({target})}));return button;}));
  const current=calibration.current_target,observation=calibration.observations[current];
  byId('overlayTarget').textContent=targetLabel(current);
  byId('overlayAngles').textContent=`PAN ${format(state.tracking.axes.pan.value)}° · TILT ${format(state.tracking.axes.tilt.value)}°`;
  byId('overlayStatus').textContent=observation?'MARKED':'NOT MARKED';
  byId('clearTarget').disabled=!observation;
  byId('markTarget').disabled=!state.tracking.valid;
  const rows=solution?{
    Status:calibration.valid&&collection.profile_match?'VALID':'NOT VALID',
    'Camera X':`${format(solution.camera.x)} m`,'Camera Y':`${format(solution.camera.y)} m`,'Camera Z':`${format(solution.camera.z)} m`,
    'Pan offset':`${format(solution.pan_offset)}°`,'Tilt offset':`${format(solution.tilt_offset)}°`,
    'RMS error':`${format(solution.rms_angular_error,4)}°`,'Max error':`${format(solution.max_angular_error,4)}°`,Observations:solution.observation_count,
  }:{Status:calibration.status,Observations:Object.keys(calibration.observations).length};
  byId('solution').replaceChildren(...Object.entries(rows).flatMap(([key,value])=>{const label=document.createElement('div'),content=document.createElement('div');label.textContent=key;content.textContent=value;return[label,content];}));
  const truth=calibration.synthetic_ground_truth;
  byId('syntheticComparison').textContent=truth&&solution?`Ground truth camera (${format(truth.camera.x)}, ${format(truth.camera.y)}, ${format(truth.camera.z)}), solved (${format(solution.camera.x)}, ${format(solution.camera.y)}, ${format(solution.camera.z)}).` : '';
}

function collectUpdate(){
  const center=Object.fromEntries(['x','y','z'].map(key=>[key,+document.querySelector(`[data-target-center="${key}"]`).value]));
  const target={center,...Object.fromEntries(['width','height','yaw'].map(key=>[key,+document.querySelector(`[data-target="${key}"]`).value]))};
  const camera_hint=Object.fromEntries(['x','y','z'].map(key=>[key,+document.querySelector(`[data-camera-hint="${key}"]`).value]));
  const orientation_hint=Object.fromEntries(['pan_offset','tilt_offset'].map(key=>[key,+document.querySelector(`[data-orientation-hint="${key}"]`).value]));
  return{name:byId('worldCalibrationName').value,target,camera_hint,orientation_hint,horizontal_fov:+byId('worldFov').value,fixed_roll:+byId('worldRoll').value};
}

connectState(incoming=>{state=incoming;applying=true;renderCalibration();byId('calibrationAxes').innerHTML=axisCard('pan')+axisCard('tilt');for(const axis of ['pan','tilt']){const runtime=state.tracking.axes[axis];if(runtime.raw!==null&&byId(axis)!==document.activeElement)byId(axis).value=runtime.raw;byId(axis).disabled=runtime.source!=='simulator';}applying=false;const panMap=state.profiles.active.axes.pan.mapping,tiltMap=state.profiles.active.axes.tilt.mapping;byId('panResult').textContent=`LEFT ${state.calibration.marks.pan_left??'—'} · RIGHT ${state.calibration.marks.pan_right??'—'} · ${panMap.direction_learned?(panMap.direction>0?'+1 learned':'-1 learned'):'not learned'}`;byId('tiltResult').textContent=`DOWN ${state.calibration.marks.tilt_down??'—'} · UP ${state.calibration.marks.tilt_up??'—'} · ${tiltMap.direction_learned?(tiltMap.direction>0?'+1 learned':'-1 learned'):'not learned'}`;},'calibration');

byId('worldCalibrationSelect').onchange=event=>safe(()=>api(`/api/world-calibrations/${event.target.value}/select`,{method:'POST'}));
byId('newWorldCalibration').onclick=()=>safe(()=>api('/api/world-calibrations',{method:'POST',body:JSON.stringify({name:'New World Calibration'})}));
byId('saveWorldCalibration').onclick=()=>safe(()=>api('/api/world-calibration',{method:'PATCH',body:JSON.stringify({values:collectUpdate()})}));
byId('previousTarget').onclick=()=>safe(()=>api('/api/world-calibration/previous',{method:'POST'}));
byId('nextTarget').onclick=()=>safe(()=>api('/api/world-calibration/next',{method:'POST'}));
byId('markTarget').onclick=()=>safe(()=>api('/api/world-calibration/mark',{method:'POST'}));
byId('clearTarget').onclick=()=>safe(()=>api(`/api/world-calibration/marks/${state.world_calibrations.active.current_target}`,{method:'DELETE'}));
byId('solveWorld').onclick=()=>safe(()=>api('/api/world-calibration/solve',{method:'POST'}));
byId('resetWorld').onclick=()=>safe(()=>api('/api/world-calibration/reset',{method:'POST'}));
byId('generateSynthetic').onclick=()=>safe(()=>api('/api/world-calibration/synthetic',{method:'POST',body:JSON.stringify({values:{camera:{x:+byId('syntheticX').value,y:+byId('syntheticY').value,z:+byId('syntheticZ').value},pan_offset:+byId('syntheticPanOffset').value,tilt_offset:+byId('syntheticTiltOffset').value}})}));
document.querySelectorAll('[data-mark]').forEach(button=>button.onclick=()=>safe(()=>api('/api/calibration/mark',{method:'POST',body:JSON.stringify({mark:button.dataset.mark})})));
byId('resetDirection').onclick=()=>safe(()=>api('/api/calibration/reset',{method:'POST'}));
for(const axis of ['pan','tilt'])byId(axis).addEventListener('input',event=>{if(!applying)safe(()=>api('/api/camera',{method:'POST',body:JSON.stringify({[axis]:+event.target.value})}));});
