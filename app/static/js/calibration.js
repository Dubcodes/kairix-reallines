import {api,connectState,gateControlsUntilState,logFrontend} from './common.js?v=world-media1';
import {calculateMovementCalibration} from './calibration_contract.js?v=world-media1';
import {updatePreviewMedia} from './preview_media.js?v=world-media1';

const byId=id=>document.getElementById(id);
const targets=['top_left','top_center','top_right','middle_left','center','middle_right','bottom_left','bottom_center','bottom_right'];
const targetLabel=value=>value.replaceAll('_',' ').toUpperCase();
let state=null,applying=false;
const movement={pan:{start:null,end:null,angle:'',proposal:null,error:''},tilt:{start:null,end:null,angle:'',proposal:null,error:''}};
const releaseInitialControls=gateControlsUntilState();
let controlsReady=false;

function format(value,digits=3){return Number.isFinite(+value)?(+value).toFixed(digits):'—';}
function error(message=''){byId('worldError').textContent=message;}
async function safe(action){try{error();return await action();}catch(reason){error(String(reason));logFrontend('calibration','world_calibration_ui_error',{error:String(reason)});return null;}}
function axisCard(axis){const runtime=state.tracking.axes[axis],mapping=state.profiles.active.axes[axis].mapping;return`<article class="axis-card"><div class="axis-heading"><h3>${axis.toUpperCase()}</h3><span class="axis-health health-${runtime.health.toLowerCase().replaceAll('_','-')}">${runtime.health}</span></div><div class="kv"><div>Source</div><div>${runtime.source}</div><div>Raw</div><div>${format(runtime.raw)}</div><div>Mapped</div><div>${runtime.value===null?'—':`${format(runtime.value)}°`}</div><div>Direction</div><div>${mapping.direction_learned?`${mapping.direction>0?'+1':'-1'} learned`:`${mapping.direction>0?'+1':'-1'} default`}</div><div>Valid</div><div>${runtime.valid?'YES':'NO'}</div></div></article>`;}
function movementCard(axis){const draft=movement[axis],config=state.profiles.active.axes[axis],runtime=state.tracking.axes[axis],mapping=config.mapping,enabled=['quadrature_gpio','linux_rotary'].includes(config.source),live=enabled&&runtime.driver_alive&&Number.isFinite(runtime.raw_count),proposal=draft.proposal;return`<article class="axis-card" data-movement-axis="${axis}"><div class="axis-heading"><h3>${axis.toUpperCase()}</h3><span class="axis-health ${live?'health-valid':'health-disabled'}">${live?'ENCODER LIVE':enabled?'ENCODER NOT LIVE':'NOT ACTIVE'}</span></div><div class="kv"><div>Current ratio</div><div>${format(mapping.encoder_revs_per_camera_rev,6)} encoder rev/camera rev</div><div>Current direction</div><div>${mapping.direction>0?'+1':'-1'}</div><div>Live raw count</div><div>${runtime.raw_count??'—'}</div><div>Start count</div><div>${draft.start??'—'}</div><div>End count</div><div>${draft.end??'—'}</div></div><div class="row"><button data-move-action="start" ${live?'':'disabled'}>Mark start</button><button data-move-action="end" ${live?'':'disabled'}>Mark end</button></div><label>Known signed camera movement (°)<input data-move-angle type="number" step="any" value="${draft.angle}" ${enabled?'':'disabled'}></label><button data-move-action="calculate" ${enabled?'':'disabled'}>Calculate proposal</button><div class="small">${proposal?`Δ counts ${proposal.delta}; encoder turns ${format(proposal.encoder_turns,6)}; camera fraction ${format(proposal.camera_fraction,6)}.<br><strong>Proposed ratio ${format(proposal.encoder_revs_per_camera_rev,6)}, direction ${proposal.direction>0?'+1':'-1'}</strong>`:'No proposal calculated.'}</div><div class="form-error">${draft.error}</div><button data-move-action="apply" class="danger" ${proposal?'':'disabled'}>Apply proposed mapping</button></article>`;}
function renderMovement(){byId('movementCalibration').innerHTML=movementCard('pan')+movementCard('tilt');}
function setInput(selector,value){const input=document.querySelector(selector);if(input&&input!==document.activeElement)input.value=value;}

function renderCalibration(){
  const collection=state.world_calibrations,calibration=collection.active,solution=calibration.solution;
  byId('activeProfile').textContent=state.profiles.active.name;
  const identityMatch=collection.profile_match&&collection.fingerprint_match&&collection.rig_match;
  const worldReady=calibration.valid&&identityMatch&&collection.reference_match;
  const mismatchReason=!collection.profile_match?'This calibration belongs to a different setup profile.':!collection.fingerprint_match?'Tracking configuration changed; update this calibration.':!collection.rig_match?'Camera rig geometry changed; solve this calibration again.':!collection.reference_match?'Encoder reference changed; update this calibration.':!calibration.valid?'World calibration has not been solved.':'';
  byId('profileMatch').textContent=worldReady?'Tracking and world calibration are valid.':state.tracking.valid?`TRACKING VALID. ${mismatchReason} Camera Preview is available using a provisional pose; calibration improves world accuracy and enables strict GFX output.`:`TRACKING INVALID. ${mismatchReason||'A required tracking axis needs attention.'}`;
  byId('profileMatch').className=worldReady?'small status-ok':state.tracking.valid?'small status-warn':'form-error';
  const select=byId('worldCalibrationSelect');
  if(select!==document.activeElement)select.replaceChildren(...collection.items.map(item=>{const option=document.createElement('option');option.value=item.id;option.textContent=item.name;option.selected=item.id===collection.active_id;return option;}));
  if(byId('worldCalibrationName')!==document.activeElement)byId('worldCalibrationName').value=calibration.name;
  for(const axis of ['x','y','z']){setInput(`[data-target-center="${axis}"]`,calibration.target.center[axis]);setInput(`[data-camera-hint="${axis}"]`,calibration.camera_hint[axis]);}
  for(const key of ['width','height','yaw'])setInput(`[data-target="${key}"]`,calibration.target[key]);
  for(const key of ['pan_offset','tilt_offset'])setInput(`[data-orientation-hint="${key}"]`,calibration.orientation_hint[key]);
  const currentRig=state.profiles.active.camera.rig_offset;
  for(const key of ['right_m','forward_m','up_m'])setInput(`[data-rig-offset="${key}"]`,currentRig[key]*1000);
  setInput('#worldFov',calibration.horizontal_fov);
  setInput('#worldRoll',calibration.fixed_roll);
  byId('status').textContent=worldReady?'CALIBRATED':state.tracking.valid?'TRACKING VALID · WORLD CALIBRATION NEEDS UPDATING':'TRACKING INVALID';
  byId('status').className=worldReady?'status-ok':state.tracking.valid?'status-warn':'status-bad';
  byId('targetSteps').replaceChildren(...targets.map((target,index)=>{const button=document.createElement('button');button.className=`target-step${target===calibration.current_target?' active':''}${calibration.observations[target]?' marked':''}`;button.textContent=`${index+1}. ${targetLabel(target)} — ${calibration.observations[target]?'MARKED':'NOT MARKED'}`;button.onclick=()=>safe(()=>api('/api/world-calibration/target',{method:'POST',body:JSON.stringify({target})}));return button;}));
  const current=calibration.current_target,observation=calibration.observations[current];
  byId('overlayTarget').textContent=targetLabel(current);
  byId('overlayAngles').textContent=`PAN ${format(state.tracking.axes.pan.value)}° · TILT ${format(state.tracking.axes.tilt.value)}°`;
  byId('overlayStatus').textContent=observation?'MARKED':'NOT MARKED';
  byId('clearTarget').disabled=!observation;
  byId('markTarget').disabled=!state.tracking.valid;
  byId('solveWorld').disabled=!state.tracking.valid||targets.some(target=>!calibration.observations[target]);
  byId('generateSynthetic').disabled=!state.tracking.valid;
  const rows=solution?{
    Status:worldReady?'VALID':mismatchReason||calibration.effective_status||'NOT VALID',
    'Tripod pivot X':`${format((solution.pivot||solution.camera).x)} m`,'Tripod pivot Y':`${format((solution.pivot||solution.camera).y)} m`,'Tripod pivot Z':`${format((solution.pivot||solution.camera).z)} m`,
    'Pan offset':`${format(solution.pan_offset)}°`,'Tilt offset':`${format(solution.tilt_offset)}°`,
    'RMS error':`${format(solution.rms_angular_error,4)}°`,'Max error':`${format(solution.max_angular_error,4)}°`,Observations:solution.observation_count,
  }:{Status:calibration.status,Observations:Object.keys(calibration.observations).length};
  byId('solution').replaceChildren(...Object.entries(rows).flatMap(([key,value])=>{const label=document.createElement('div'),content=document.createElement('div');label.textContent=key;content.textContent=value;return[label,content];}));
  const solvedRig=solution?.rig_offset;
  const rigRows={};
  for(const [key,label] of [['right_m','Right'],['forward_m','Forward'],['up_m','Up']]){
    const solved=solvedRig?.[key];
    rigRows[`${label} measured/current`]=`${format(currentRig[key]*1000,1)} mm`;
    rigRows[`${label} solved`]=solved===undefined?'—':`${format(solved*1000,1)} mm`;
    rigRows[`${label} difference`]=solved===undefined?'—':`${format((solved-currentRig[key])*1000,1)} mm`;
  }
  byId('rigSolution').replaceChildren(...Object.entries(rigRows).flatMap(([key,value])=>{const label=document.createElement('div'),content=document.createElement('div');label.textContent=key;content.textContent=value;return[label,content];}));
  byId('applySolvedRig').disabled=!solvedRig;
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

connectState((incoming,event)=>{state=incoming;if(!controlsReady){releaseInitialControls();controlsReady=true;}applying=true;updatePreviewMedia(byId('calibrationMedia'),state.engineering,true);if(event?.type!=='pose'){renderCalibration();renderMovement();}else byId('overlayAngles').textContent=`PAN ${format(state.tracking.axes.pan.value)}° · TILT ${format(state.tracking.axes.tilt.value)}°`;byId('calibrationAxes').innerHTML=axisCard('pan')+axisCard('tilt');for(const axis of ['pan','tilt']){const runtime=state.tracking.axes[axis];if(runtime.raw!==null&&byId(axis)!==document.activeElement)byId(axis).value=runtime.raw;byId(axis).disabled=runtime.source!=='simulator';document.querySelectorAll(`[data-mark^="${axis}_"]`).forEach(button=>button.disabled=!runtime.valid);}const observations=state.world_calibrations.active.observations;byId('markTarget').disabled=!state.tracking.valid;byId('solveWorld').disabled=!state.tracking.valid||targets.some(target=>!observations[target]);byId('generateSynthetic').disabled=!state.tracking.valid;applying=false;if(event?.type!=='pose'){const panMap=state.profiles.active.axes.pan.mapping,tiltMap=state.profiles.active.axes.tilt.mapping;byId('panResult').textContent=`LEFT ${state.calibration.marks.pan_left??'—'} · RIGHT ${state.calibration.marks.pan_right??'—'} · ${panMap.direction_learned?(panMap.direction>0?'+1 learned':'-1 learned'):'not learned'}`;byId('tiltResult').textContent=`DOWN ${state.calibration.marks.tilt_down??'—'} · UP ${state.calibration.marks.tilt_up??'—'} · ${tiltMap.direction_learned?(tiltMap.direction>0?'+1 learned':'-1 learned'):'not learned'}`;}},'calibration');

byId('movementCalibration').addEventListener('input',event=>{const card=event.target.closest('[data-movement-axis]');if(card&&event.target.matches('[data-move-angle]'))movement[card.dataset.movementAxis].angle=event.target.value;});
byId('movementCalibration').addEventListener('click',async event=>{const button=event.target.closest('[data-move-action]'),card=button?.closest('[data-movement-axis]');if(!button||!card)return;const axis=card.dataset.movementAxis,draft=movement[axis],runtime=state.tracking.axes[axis],mapping=state.profiles.active.axes[axis].mapping;draft.error='';try{if(button.dataset.moveAction==='start')draft.start=runtime.raw_count;if(button.dataset.moveAction==='end')draft.end=runtime.raw_count;if(button.dataset.moveAction==='calculate')draft.proposal=calculateMovementCalibration({start_count:draft.start,end_count:draft.end,actual_angle:draft.angle,ppr:mapping.ppr,multiplier:mapping.quadrature_multiplier});if(button.dataset.moveAction==='apply'){await api(`/api/profiles/${state.profiles.active_id}/axes/${axis}`,{method:'PATCH',body:JSON.stringify({values:{mapping:{direction:draft.proposal.direction,direction_learned:true,encoder_revs_per_camera_rev:draft.proposal.encoder_revs_per_camera_rev}}})});draft.proposal=null;draft.start=null;draft.end=null;draft.error='REFERENCE REQUIRED — Set Zero or a known angle again, then re-solve world calibration.';}}catch(reason){draft.error=String(reason.message||reason);}renderMovement();});

byId('worldCalibrationSelect').onchange=event=>safe(()=>api(`/api/world-calibrations/${event.target.value}/select`,{method:'POST'}));
byId('newWorldCalibration').onclick=()=>safe(()=>api('/api/world-calibrations',{method:'POST',body:JSON.stringify({name:'New World Calibration'})}));
byId('saveWorldCalibration').onclick=()=>safe(()=>api('/api/world-calibration',{method:'PATCH',body:JSON.stringify({values:collectUpdate()})}));
byId('saveRigGeometry').onclick=()=>safe(()=>api(`/api/profiles/${state.profiles.active_id}/camera-rig`,{method:'PATCH',body:JSON.stringify({values:Object.fromEntries(['right_m','forward_m','up_m'].map(key=>[key,+document.querySelector(`[data-rig-offset="${key}"]`).value/1000]))})}));
byId('applySolvedRig').onclick=()=>safe(()=>api(`/api/profiles/${state.profiles.active_id}/camera-rig`,{method:'PATCH',body:JSON.stringify({values:state.world_calibrations.active.solution.rig_offset})}));
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
