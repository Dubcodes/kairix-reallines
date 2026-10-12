import {api,connectState,logFrontend} from './common.js?v=world-media1';
import {
  ENGINEERING_AXES,
  buildEncoderSourceConfig,
  buildSourceUpdate,
  sourceChoicesFor,
  validateEngineeringState,
} from './engineering_contract.js?v=world-media1';

window.__kairixEngineeringInitialized=true;
const byId=id=>document.getElementById(id);
const axes=ENGINEERING_AXES;
const dialog=byId('engineeringDialog');
let state=null,applying=false,axesKey='',connectionMode='connecting';
const setUiStatus=(label,detail='')=>window.__kairixEngineeringBoot?.(label,detail);

if(dialog&&byId('dialogCancel'))byId('dialogCancel').onclick=()=>dialog.close('cancel');
function showDialog({title,message='',value=null,accept='OK'}){if(!dialog)return Promise.resolve({accepted:false});byId('dialogTitle').textContent=title;byId('dialogMessage').textContent=message;byId('dialogInput').hidden=value===null;byId('dialogInput').value=value??'';byId('dialogAccept').textContent=accept;return new Promise(resolve=>{dialog.addEventListener('close',()=>resolve(dialog.returnValue==='accept'?{accepted:true,value:byId('dialogInput').value.trim()}:{accepted:false}),{once:true});dialog.showModal();if(value!==null){byId('dialogInput').focus();byId('dialogInput').select();}});}
function format(value,digits=2){return Number.isFinite(+value)?(+value).toFixed(digits):'—';}
function escapeHtml(value){return String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));}
function setError(message=''){byId('profileError').textContent=message;}
async function safe(action){try{setError();return await action();}catch(error){setError(String(error));logFrontend('engineering','tracking_ui_error',{error:String(error)});return null;}}

function renderProfiles(){const select=byId('profileSelect'),active=state.profiles.active_id;select.disabled=false;byId('profileName').disabled=false;for(const id of ['newProfile','duplicateProfile','renameProfile'])byId(id).disabled=false;if(select!==document.activeElement){select.replaceChildren(...state.profiles.items.map(meta=>{const option=document.createElement('option');option.value=meta.id;option.textContent=meta.name;option.selected=meta.id===active;return option;}));}if(byId('profileName')!==document.activeElement)byId('profileName').value=state.profiles.active.name;byId('deleteProfile').disabled=state.profiles.items.length<=1;}
function sourceHelp(source){return({linux_rotary:'Uses the Raspberry Pi kernel encoder driver at full ×4 resolution.',quadrature_gpio:'Reads every encoder edge directly in RealLines. Useful for testing and diagnostics.',simulator:'Software movement for setup and testing; no tracking hardware is used.',disabled:'This axis is off.'})[source]||'';}
function axisMarkup(axis,config){
  const isAngle=axis==='pan'||axis==='tilt';
  const gpioEncoder=config.source==='quadrature_gpio';
  const rotaryEncoder=config.source==='linux_rotary';
  const encoder=gpioEncoder||rotaryEncoder;
  const sc=config.source_config||{};
  const mapping=config.mapping;
  const degreesPerCount=360/(Number(mapping.ppr)*Number(mapping.quadrature_multiplier)*Number(mapping.encoder_revs_per_camera_rev));
  const sourceOptions=sourceChoicesFor(axis).map(choice=>`<option value="${choice.value}"${config.source===choice.value?' selected':''}${choice.enabled?'':' disabled'}>${choice.label}</option>`).join('');
  return `<article class="axis-card" data-axis="${axis}">
    <div class="axis-heading"><h3>${axis.toUpperCase()}</h3><span class="axis-health" data-runtime="health"></span></div>
    <label>Tracking source<select data-config="source">${sourceOptions}</select></label>
    <div class="small">${sourceHelp(config.source)}</div>
    <div class="axis-readout"><span>Encoder count <strong data-runtime="raw">—</strong></span><span>Camera angle <strong data-runtime="value">—</strong></span><span>Last update <strong data-runtime="age">—</strong></span></div>
    ${isAngle?`<div class="axis-config">
      <label>Direction<select data-mapping="direction"><option value="1"${mapping.direction===1?' selected':''}>Normal</option><option value="-1"${mapping.direction===-1?' selected':''}>Reversed</option></select></label>
      <label>Angle offset (°)<input data-mapping="offset" type="number" step="0.01" value="${mapping.offset}"></label>
      <div class="encoder-config"${encoder?'':' hidden'}>
        <label>Encoder pulses per revolution (PPR)<input data-mapping="ppr" type="number" min="1" step="1" value="${mapping.ppr}"></label>
        <div class="small">For the RealLines 600 PPR encoders, enter 600.</div>
        <label>Encoder resolution<select data-mapping="quadrature_multiplier"${rotaryEncoder?' disabled':''}>${[1,2,4].map(value=>`<option value="${value}"${mapping.quadrature_multiplier===value?' selected':''}>×${value}${value===4?' — Full resolution':''}</option>`).join('')}</select></label>
        <label>Gear ratio — encoder turns per full camera turn<input data-mapping="encoder_revs_per_camera_rev" type="number" min="0.0001" step="0.01" value="${mapping.encoder_revs_per_camera_rev}"></label>
        <div class="small">Example: 10 means the encoder turns 10 times while the camera turns 360°.</div>
        <div class="small">Tracking resolution: <strong>${format(degreesPerCount,6)}° per encoder count</strong></div>
        <div class="gpio-draft"${gpioEncoder?'':' hidden'}>
          <label>gpiochip device<input data-source-config="chip" placeholder="/dev/gpiochip0" value="${escapeHtml(sc.chip||'')}"></label>
          <label>Line A offset<input data-source-config="line_a" type="number" min="0" step="1" value="${sc.line_a??''}"></label>
          <label>Line B offset<input data-source-config="line_b" type="number" min="0" step="1" value="${sc.line_b??''}"></label>
          <label>Bias<select data-source-config="bias">${[['as_is','As-is'],['pull_up','Pull-up'],['pull_down','Pull-down']].map(([value,label])=>`<option value="${value}"${(sc.bias||'as_is')===value?' selected':''}>${label}</option>`).join('')}</select></label>
          <div class="row"><button data-apply-gpio type="button">Apply GPIO settings</button><strong data-gpio-save-state class="status-ok">SAVED</strong></div>
        </div>
        <div class="small"${rotaryEncoder?'':' hidden'}>Expected wiring: A GPIO${sc.pin_a??'—'} · B GPIO${sc.pin_b??'—'}. Requires the Raspberry Pi encoder device-tree overlay and exclusive ownership of these pins. Reboot after enabling it.</div>
        <div class="reference-guidance" data-runtime="reference-guidance"></div>
        <div class="row"><button data-reference="set" type="button">Set current angle…</button><button data-reference="zero" type="button" title="This tells RealLines that the camera is currently at 0° on this axis.">Set this position as 0°</button><button data-reference="clear" type="button">Clear position reference</button></div>
        <div class="form-error encoder-alert" data-runtime="encoder-alert"></div>
        <details class="technical-details"><summary>Advanced encoder diagnostics</summary><div class="small" data-runtime="encoder-diagnostics"></div></details>
      </div>
    </div>`:''}
    <div class="form-error" data-runtime="error"></div>
  </article>`;
}

function setAxisError(card,message=''){
  card.querySelector('[data-runtime="error"]').textContent=message;
}

async function applyGpioDraft(axis,card){
  const saveState=card.querySelector('[data-gpio-save-state]');
  const draft=Object.fromEntries([...card.querySelectorAll('[data-source-config]')].map(input=>[input.dataset.sourceConfig,input.value]));
  try {
    const values=buildEncoderSourceConfig(draft);
    setAxisError(card);
    saveState.textContent='APPLYING';
    saveState.className='status-warn';
    const result=await api(`/api/profiles/${state.profiles.active_id}/axes/${axis}`,{
      method:'PATCH',
      body:JSON.stringify({values}),
    });
    state.profiles.active.axes[axis]=result;
    axesKey='';
    renderAxes();
  } catch(error) {
    const message=String(error);
    setAxisError(card,message);
    saveState.textContent='UNSAVED';
    saveState.className='status-bad';
    logFrontend('engineering','gpio_config_error',{axis,error:message});
  }
}

function bindAxisCards(){
  byId('trackingAxes').querySelectorAll('.axis-card').forEach(card=>{
    const axis=card.dataset.axis;
    card.querySelector('[data-config="source"]').addEventListener('change',event=>{
      updateAxis(axis,buildSourceUpdate(event.target.value));
    });

    card.querySelectorAll('[data-mapping]').forEach(input=>{
      const commit=()=>{
        const value=Number(input.value);
        if(Number.isFinite(value))updateAxis(axis,{mapping:{[input.dataset.mapping]:value}});
      };
      input.addEventListener('change',commit);
      input.addEventListener('keydown',event=>{
        if(event.key==='Enter'){
          event.preventDefault();
          input.blur();
        }
      });
    });

    const saveState=card.querySelector('[data-gpio-save-state]');
    card.querySelectorAll('[data-source-config]').forEach(input=>input.addEventListener('input',()=>{
      saveState.textContent='UNSAVED';
      saveState.className='status-warn';
      setAxisError(card);
    }));
    card.querySelector('[data-apply-gpio]')?.addEventListener('click',()=>applyGpioDraft(axis,card));

    card.querySelectorAll('[data-reference]').forEach(button=>{
      button.onclick=async()=>{
        const action=button.dataset.reference;
        if(action==='clear'){
          return safe(()=>api(`/api/tracking/${axis}/reference`,{method:'DELETE'}));
        }
        let angle=0;
        if(action==='set'){
          const axisName=axis[0].toUpperCase()+axis.slice(1);
          const answer=await showDialog({title:`Set current ${axisName} angle`,message:`Enter the real camera ${axisName} angle at its current physical position.`,value:'0',accept:'Set camera position'});
          if(!answer.accepted)return;
          angle=Number(answer.value);
          if(!Number.isFinite(angle))return setError('Reference angle must be numeric');
        }
        return safe(()=>api(`/api/tracking/${axis}/reference`,{
          method:'POST',
          body:JSON.stringify({angle}),
        }));
      };
    });
  });
}

async function updateAxis(axis,values){
  const card=byId('trackingAxes').querySelector(`[data-axis="${axis}"]`);
  const result=await safe(()=>api(`/api/profiles/${state.profiles.active_id}/axes/${axis}`,{
    method:'PATCH',
    body:JSON.stringify({values}),
  }));
  if(result){
    state.profiles.active.axes[axis]=result;
    axesKey='';
    renderAxes();
  }else if(card){
    setAxisError(card,byId('profileError').textContent);
  }
}
function renderAxes(){
  const configKey=state.profiles.active_id+JSON.stringify(state.profiles.active.axes);
  if(configKey!==axesKey){axesKey=configKey;byId('trackingAxes').innerHTML=axes.map(axis=>axisMarkup(axis,state.profiles.active.axes[axis])).join('');bindAxisCards();}
  for(const axis of axes){
    const card=byId('trackingAxes').querySelector(`[data-axis="${axis}"]`),runtime=state.tracking.axes[axis];if(!card)continue;
    const health=card.querySelector('[data-runtime="health"]');health.textContent=({VALID:'READY',REFERENCE_REQUIRED:'SET CAMERA POSITION',DISABLED:'OFF',ERROR:'HARDWARE ERROR',STALE:'HARDWARE ERROR',NOT_CONFIGURED:'SETUP NEEDED'})[runtime.health]||runtime.health.replaceAll('_',' ');health.className=`axis-health health-${runtime.health.toLowerCase().replaceAll('_','-')}`;
    card.querySelector('[data-runtime="raw"]').textContent=format(runtime.raw,3);
    card.querySelector('[data-runtime="value"]').textContent=runtime.value===null?'—':`${format(runtime.value,3)} ${runtime.unit==='deg'?'°':''}`;
    card.querySelector('[data-runtime="age"]').textContent=runtime.age_seconds===null?'—':`${format(runtime.age_seconds*1000,0)} ms`;
    for(const button of card.querySelectorAll('[data-reference]'))button.disabled=button.dataset.reference==='clear'?!runtime.referenced:!runtime.driver_alive;
    const guidance=card.querySelector('[data-runtime="reference-guidance"]');
    if(guidance){
      guidance.className=`reference-guidance ${runtime.referenced?'reference-ready':'reference-required'}`;
      guidance.innerHTML=runtime.referenced
        ? '<strong>POSITION SET</strong><span>RealLines knows this camera angle.</span>'
        : '<strong>SET CAMERA POSITION</strong><span>RealLines knows the encoder is moving but does not yet know the camera’s current angle.</span>';
    }
    const diagnostics=card.querySelector('[data-runtime="encoder-diagnostics"]');
    if(diagnostics){const d=runtime.diagnostics||{},rotary=runtime.source==='linux_rotary',relative=runtime.relative_angle===null||runtime.relative_angle===undefined?'—':`${format(runtime.relative_angle,3)}°`,edgeAge=d.last_event_age_ms??d.last_edge_age_ms,lastEdge=edgeAge===null||edgeAge===undefined?'—':`${format(edgeAge,1)} ms`,referenceId=runtime.reference_id?escapeHtml(String(runtime.reference_id).slice(0,12)):'—',sourceDetails=rotary?`<div>Kernel driver</div><div>${escapeHtml(d.kernel_driver||'rotary-encoder')}</div><div>Resolution mode</div><div>×4 quarter-period</div><div>REL axis</div><div>${escapeHtml(d.rel_axis||'—')}</div><div>Device identity</div><div>${escapeHtml(d.device_identity||'—')}</div><div>Resolved device</div><div>${escapeHtml(d.resolved_event_device||'—')}</div><div>SYN_DROPPED</div><div>${d.evdev_dropped_events??0}</div>`:`<div>A state</div><div>${d.a_state??'—'}</div><div>B state</div><div>${d.b_state??'—'}</div><div>Requested GPIO event buffer</div><div>${d.kernel_event_buffer_size??'—'} (actual kernel capacity may be lower/capped)</div><div>Read batch size</div><div>${d.read_batch_size??'—'}</div><div>Confirmed global gaps</div><div>${d.confirmed_global_gaps??d.global_sequence_gaps??0}</div><div>Global-only gaps</div><div>${d.global_only_sequence_gaps??0}</div><div>Global out-of-order</div><div>${d.global_out_of_order_events??0}</div><div>Line sequence gaps</div><div>${d.line_sequence_gaps??0}</div><div>Illegal transitions</div><div>${d.illegal_transitions??0}</div>`;diagnostics.className='kv encoder-diagnostics';diagnostics.innerHTML=`${sourceDetails}<div>Raw count</div><div>${runtime.raw_count??'—'}</div><div>Reference count</div><div>${runtime.reference_count??'—'}</div><div>Relative angle</div><div>${relative}</div><div>Mapped angle</div><div>${runtime.value===null?'—':`${format(runtime.value,3)}°`}</div><div>Reference</div><div>${runtime.referenced?'REFERENCED':'REFERENCE REQUIRED'}</div><div>Reference angle</div><div>${runtime.referenced?`${format(runtime.reference_angle,3)}°`:'—'}</div><div>Reference ID</div><div title="${escapeHtml(runtime.reference_id||'')}">${referenceId}</div><div>Direction value</div><div>${state.profiles.active.axes[axis].mapping.direction>0?'+1':'-1'}</div><div>Events/sec</div><div>${format(d.event_rate_hz??0,1)}</div><div>Peak events/sec</div><div>${format(d.peak_event_rate_hz??0,1)}</div><div>Total movement events</div><div>${d.events??0}</div><div>Counts/sec</div><div>${format(d.counts_per_second??0,1)}</div><div>Degrees/sec</div><div>${format(d.degrees_per_second??0,2)}°</div><div>Integrity-loss events</div><div>${d.integrity_loss_events??0}</div><div>Integrity</div><div>${d.integrity_status??'—'}</div><div>Last event age</div><div>${lastEdge}</div><div>Driver restarts</div><div>${d.restarts??0}</div><div>Driver status</div><div>${escapeHtml(runtime.status||'—')}</div><div>Last driver error</div><div>${escapeHtml(d.last_error||'—')}</div>`;const alert=card.querySelector('[data-runtime="encoder-alert"]');if(alert)alert.textContent=(d.integrity_lost||Number(d.evdev_dropped_events)>0)?'Encoder data was lost. Set the camera position again.':runtime.health==='ERROR'?`Encoder hardware error: ${runtime.status||'check the connection and driver.'}`:'';}
    card.title=runtime.status||'';
  }
}
function renderSync(){const sync=state.sync,live=state.camera,render=state.render_camera,rows={'Acquisition target':'200 Hz','Measured history rate':sync.sample_rate_hz===null?'—':`${format(sync.sample_rate_hz,1)} Hz`,'Pose transport target':'50 Hz','Configured delay':`${sync.graphics_delay_ms} ms`,'History depth':`${sync.history_duration_seconds} s`,'Stored samples':sync.sample_count,'Oldest sample age':sync.oldest_age_ms===null?'—':`${format(sync.oldest_age_ms,1)} ms`,'Newest sample age':sync.newest_age_ms===null?'—':`${format(sync.newest_age_ms,1)} ms`,'Requested render age':`${sync.requested_render_age_ms} ms`,'Interpolation':sync.interpolation,'Live Pan / Tilt':`${format(live.pan,3)}° / ${format(live.tilt,3)}°`,'Render Pan / Tilt':`${format(render.pan,3)}° / ${format(render.tilt,3)}°`,'Effective difference':sync.effective_difference_ms===null?'—':`${format(sync.effective_difference_ms,1)} ms`};byId('syncDiagnostics').replaceChildren(...Object.entries(rows).flatMap(([key,value])=>{const label=document.createElement('div'),content=document.createElement('div');label.textContent=key;content.textContent=value;return[label,content];}));}
function renderSetupStatus(){
  const required=['pan','tilt'],hardwareSources=['linux_rotary','quadrature_gpio'];
  const hardware=required.every(axis=>hardwareSources.includes(state.profiles.active.axes[axis].source));
  const drivers=hardware&&required.every(axis=>state.tracking.axes[axis].driver_alive);
  const referenced=drivers&&required.every(axis=>state.tracking.axes[axis].referenced);
  const rotaryMissing=required.some(axis=>state.profiles.active.axes[axis].source==='linux_rotary'&&!state.tracking.axes[axis].driver_alive);
  let message;
  if(!hardware)message='Choose a Pan and Tilt encoder source.';
  else if(!drivers)message=rotaryMissing?'Encoder driver not detected — enable the Raspberry Pi encoder overlay and reboot.':'Encoder hardware is not ready — check its connection and settings.';
  else if(!referenced)message='Both encoders are connected. Set the current Pan and Tilt position.';
  else if(!state.tracking.valid)message='Camera tracking needs setup. Check the Pan and Tilt status below.';
  else if(!state.camera.world_valid)message='Camera movement is working. Finish camera/world calibration.';
  else message='Camera tracking is ready.';
  byId('setupSummary').textContent=message;
  const stages={
    setupHardware:drivers?{label:'Ready',status:'ok'}:{label:'Next step',status:'warn'},
    setupReference:referenced?{label:'Ready',status:'ok'}:{label:'Next step',status:'warn'},
    setupCalibration:state.camera.world_valid?{label:'Ready',status:'ok'}:{label:'Next step',status:'warn'},
    setupGraphics:{label:'Available',status:'ok'},
    setupOutput:state.camera.world_valid||state.engineering.allow_provisional_gfx?{label:'Ready',status:'ok'}:{label:'Waiting',status:'warn'},
  };
  for(const [id,stage] of Object.entries(stages)){const element=byId(id);element.textContent=stage.label;element.className=`status-${stage.status}`;}
}

const simulatorIds=['pan','tilt','fov','height'];
function showSlider(id,value){byId(id+'Out').textContent=format(value,id==='height'?2:1)+(id==='height'?' m':id==='fov'?'°':' raw');}
function sourceLabel(source){return({quadrature_gpio:'Direct GPIO Encoder',linux_rotary:'Raspberry Pi Encoder',simulator:'Simulator',disabled:'Off',imu:'IMU',external:'External Tracker'})[source]||source;}
for(const id of simulatorIds){const input=byId(id),eventName=id==='pan'||id==='tilt'?'input':'change';input.addEventListener('input',()=>showSlider(id,input.value));input.addEventListener(eventName,()=>{showSlider(id,input.value);if(!applying)safe(()=>api('/api/camera',{method:'POST',body:JSON.stringify({[id]:+input.value})}));});showSlider(id,input.value);}

byId('profileSelect').onchange=event=>safe(()=>api(`/api/profiles/${event.target.value}/select`,{method:'POST'}));
byId('newProfile').onclick=async()=>{const answer=await showDialog({title:'Create setup profile',value:'New Setup Profile',accept:'Create'});if(answer.accepted&&answer.value)await safe(()=>api('/api/profiles',{method:'POST',body:JSON.stringify({name:answer.value})}));};
byId('renameProfile').onclick=async()=>{const answer=await showDialog({title:'Rename setup profile',value:byId('profileName').value,accept:'Rename'});if(answer.accepted&&answer.value)await safe(()=>api(`/api/profiles/${state.profiles.active_id}`,{method:'PATCH',body:JSON.stringify({name:answer.value})}));};
byId('duplicateProfile').onclick=async()=>{const answer=await showDialog({title:'Duplicate setup profile',value:`${state.profiles.active.name} Copy`,accept:'Duplicate'});if(answer.accepted&&answer.value)await safe(()=>api(`/api/profiles/${state.profiles.active_id}/duplicate`,{method:'POST',body:JSON.stringify({name:answer.value})}));};
byId('deleteProfile').onclick=async()=>{const answer=await showDialog({title:`Delete “${state.profiles.active.name}”?`,message:'At least one setup profile is always retained.',accept:'Delete'});if(answer.accepted)await safe(()=>api(`/api/profiles/${state.profiles.active_id}`,{method:'DELETE'}));};
byId('debugOn').onclick=()=>safe(()=>api('/api/debug/true',{method:'POST'}));byId('debugOff').onclick=()=>safe(()=>api('/api/debug/false',{method:'POST'}));
byId('allowProvisionalGfx').onchange=async event=>{const enabled=event.target.checked,result=await safe(()=>api('/api/engineering',{method:'PATCH',body:JSON.stringify({values:{allow_provisional_gfx:enabled}})}));if(!result)event.target.checked=state.engineering.allow_provisional_gfx;};
byId('savePreviewMedia').onclick=async()=>{byId('previewMediaError').textContent='';const result=await safe(()=>api('/api/engineering',{method:'PATCH',body:JSON.stringify({values:{preview_source:byId('previewSource').value,preview_url:byId('previewUrl').value}})}));if(!result)byId('previewMediaError').textContent='Preview settings were not saved.';};

function renderState(incoming,event){
  try {
    state=validateEngineeringState(incoming);
    applying=true;
    for(const [id,runtime] of [['pan',state.tracking.axes.pan],['tilt',state.tracking.axes.tilt]]){
      const simulator=runtime.source==='simulator';
      byId(id).disabled=!simulator;
      if(simulator&&runtime.raw!==null&&byId(id)!==document.activeElement){byId(id).value=runtime.raw;showSlider(id,runtime.raw);}
      if(!simulator)byId(id+'Out').textContent=`Simulator inactive — ${sourceLabel(runtime.source)} source active`;
    }
    for(const [id,value] of [['fov',state.camera.fov],['height',state.profiles.active.camera.height]])if(value!==null&&byId(id)!==document.activeElement){byId(id).value=value;showSlider(id,value);}
    byId('fov').disabled=false;byId('height').disabled=false;
    const gfxToggle=byId('allowProvisionalGfx');
    gfxToggle.disabled=false;
    if(gfxToggle!==document.activeElement)gfxToggle.checked=state.engineering.allow_provisional_gfx===true;
    const gfxStatus=byId('gfxProjectionStatus');
    gfxStatus.textContent=state.camera.world_valid?'Graphics output ready':state.tracking.valid&&state.engineering.allow_provisional_gfx?'Testing output enabled — venue calibration incomplete':state.tracking.valid?'Finish camera/world calibration to enable accurate graphics output':'Graphics output waiting for camera tracking';
    gfxStatus.className=state.camera.world_valid?'status-ok':state.tracking.valid&&state.engineering.allow_provisional_gfx?'status-warn':'status-bad';
    for(const id of ['previewSource','previewUrl','savePreviewMedia'])byId(id).disabled=false;
    if(byId('previewSource')!==document.activeElement)byId('previewSource').value=state.engineering.preview_source||'none';
    if(byId('previewUrl')!==document.activeElement)byId('previewUrl').value=state.engineering.preview_url||'';
    applying=false;
    byId('trackingStatus').textContent=state.tracking.valid?(state.camera.world_valid?'Camera tracking ready':'Venue calibration required'):'Camera tracking needs setup';
    byId('trackingStatus').className=state.tracking.valid?(state.camera.world_valid?'status-ok':'status-warn'):'status-bad';
    if(event?.type!=='pose')renderProfiles();
    renderAxes();renderSync();renderSetupStatus();
    const world=state.world_calibrations.active,solution=world.solution;
    byId('live').innerHTML=`<div>Profile</div><div>${escapeHtml(state.profiles.active.name)}</div><div>World calibration</div><div>${escapeHtml(world.name)} · ${state.camera.world_valid?'VALID':'NOT VALID'}</div><div>RMS</div><div>${solution?`${format(solution.rms_angular_error,4)}°`:'—'}</div><div>Camera XYZ</div><div>${format(state.camera.x,2)}, ${format(state.camera.y,2)}, ${format(state.camera.z,2)} m</div><div>Pan</div><div>${format(state.camera.pan,3)}°</div><div>Tilt</div><div>${format(state.camera.tilt,3)}°</div><div>FOV</div><div>${format(state.camera.fov,2)}°</div><div>Clients</div><div>${state.clients}</div>`;
    byId('debugState').textContent=`Debug: ${state.engineering.debug_enabled?'enabled':'disabled'}`;
    if(connectionMode==='websocket')setUiStatus('Connected');
    if(connectionMode==='polling')setUiStatus('POLLING FALLBACK','WebSocket unavailable; HTTP polling is active while retrying.');
    if(connectionMode==='connecting')setUiStatus('State received · connecting');
  } catch(error) {
    applying=false;
    setUiStatus('UI ERROR',String(error));
    logFrontend('engineering','state_handler_error',{error:String(error)});
    throw error;
  }
}

function connectionStatus(status,detail){
  if(status==='connecting'&&!state)setUiStatus(window.__kairixEngineeringLoadingLabel);
  if(status==='websocket-open'){connectionMode='websocket';setUiStatus(state?'Connected':'Connected — waiting for state');}
  if(status==='websocket-error'){connectionMode='retrying';setUiStatus('WEBSOCKET ERROR / RETRYING',detail);}
  if(status==='polling'){connectionMode='polling';setUiStatus('POLLING FALLBACK',detail||'WebSocket unavailable; HTTP polling is active while retrying.');}
  if(status==='state-error'&&!state)setUiStatus('STATE ERROR',detail);
  if(status==='state-received'&&state){
    if(connectionMode==='websocket')setUiStatus('Connected');
    if(connectionMode==='polling')setUiStatus('POLLING FALLBACK','WebSocket unavailable; HTTP polling is active while retrying.');
  }
}

connectState(renderState,'engineering',connectionStatus);
async function refreshSystem(){try{const system=await api('/api/system');byId('system').innerHTML=Object.entries(system).map(([key,value])=>`<div>${escapeHtml(key)}</div><div>${escapeHtml(value)}</div>`).join('');}catch(error){byId('system').textContent=`System information unavailable: ${error}`;logFrontend('engineering','system_fetch_error',{error:String(error)});}}refreshSystem();setInterval(refreshSystem,5000);
