import {api,connectState,logFrontend} from './common.js?v=world-media1';
import {
  ENGINEERING_AXES,
  buildEncoderSourceConfig,
  sourceChoicesFor,
  validateEngineeringState,
} from './engineering_contract.js?v=world-media1';

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
    <label>Source<select data-config="source">${sourceOptions}</select></label>
    <div class="axis-readout"><span>Raw <strong data-runtime="raw">—</strong></span><span>Mapped <strong data-runtime="value">—</strong></span><span>Age <strong data-runtime="age">—</strong></span></div>
    ${isAngle?`<div class="axis-config">
      <label>Direction<select data-mapping="direction"><option value="1"${mapping.direction===1?' selected':''}>+1</option><option value="-1"${mapping.direction===-1?' selected':''}>-1</option></select></label>
      <label>Offset °<input data-mapping="offset" type="number" step="0.01" value="${mapping.offset}"></label>
      <div class="encoder-config"${encoder?'':' hidden'}>
        <label>Encoder PPR<input data-mapping="ppr" type="number" min="1" step="1" value="${mapping.ppr}"></label>
        <label>Quadrature decode<select data-mapping="quadrature_multiplier"${rotaryEncoder?' disabled':''}>${[1,2,4].map(value=>`<option value="${value}"${mapping.quadrature_multiplier===value?' selected':''}>×${value}</option>`).join('')}</select></label>
        <label>Encoder shaft revs per 1 camera revolution<input data-mapping="encoder_revs_per_camera_rev" type="number" min="0.0001" step="0.01" value="${mapping.encoder_revs_per_camera_rev}"></label>
        <div class="small">Example: 10 means the encoder shaft turns 10 times while the camera pans 360°.</div>
        <div class="small">Degrees per count: <strong>${format(degreesPerCount,6)}° / count</strong></div>
        <div class="gpio-draft"${gpioEncoder?'':' hidden'}>
          <label>gpiochip device<input data-source-config="chip" placeholder="/dev/gpiochip0" value="${escapeHtml(sc.chip||'')}"></label>
          <label>Line A offset<input data-source-config="line_a" type="number" min="0" step="1" value="${sc.line_a??''}"></label>
          <label>Line B offset<input data-source-config="line_b" type="number" min="0" step="1" value="${sc.line_b??''}"></label>
          <label>Bias<select data-source-config="bias">${[['as_is','As-is'],['pull_up','Pull-up'],['pull_down','Pull-down']].map(([value,label])=>`<option value="${value}"${(sc.bias||'as_is')===value?' selected':''}>${label}</option>`).join('')}</select></label>
          <div class="row"><button data-apply-gpio type="button">Apply GPIO settings</button><strong data-gpio-save-state class="status-ok">SAVED</strong></div>
        </div>
        <div class="kv"${rotaryEncoder?'':' hidden'}><div>Kernel driver</div><div>rotary-encoder</div><div>Resolution</div><div>×4 quarter-period</div><div>Expected pins</div><div>A GPIO${sc.pin_a??'—'} · B GPIO${sc.pin_b??'—'}</div><div>REL axis</div><div>${escapeHtml(sc.rel_axis||'—')}</div><div>Discovery</div><div>${escapeHtml(sc.discovery||'auto')}</div></div>
        <div class="small"${rotaryEncoder?'':' hidden'}>Requires the Raspberry Pi rotary-encoder device-tree overlay and exclusive ownership of these GPIO pins. Reboot after enabling the overlay.</div>
        <div class="reference-guidance" data-runtime="reference-guidance"></div>
        <div class="row"><button data-reference="set" type="button">Set current angle</button><button data-reference="zero" type="button">Zero</button><button data-reference="clear" type="button">Clear reference</button></div>
        <div class="small" data-runtime="encoder-diagnostics"></div>
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
      updateAxis(axis,{source:event.target.value});
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
          const answer=await showDialog({title:`Reference ${axis}`,message:'Enter the known current camera angle in degrees.',value:'0',accept:'Set reference'});
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
    const health=card.querySelector('[data-runtime="health"]');health.textContent=runtime.health;health.className=`axis-health health-${runtime.health.toLowerCase().replaceAll('_','-')}`;
    card.querySelector('[data-runtime="raw"]').textContent=format(runtime.raw,3);
    card.querySelector('[data-runtime="value"]').textContent=runtime.value===null?'—':`${format(runtime.value,3)} ${runtime.unit==='deg'?'°':''}`;
    card.querySelector('[data-runtime="age"]').textContent=runtime.age_seconds===null?'—':`${format(runtime.age_seconds*1000,0)} ms`;
    for(const button of card.querySelectorAll('[data-reference]'))button.disabled=button.dataset.reference==='clear'?!runtime.referenced:!runtime.driver_alive;
    const guidance=card.querySelector('[data-runtime="reference-guidance"]');
    if(guidance){
      guidance.className=`reference-guidance ${runtime.referenced?'reference-ready':'reference-required'}`;
      guidance.innerHTML=runtime.referenced
        ? '<strong>REFERENCED</strong><span>Camera tracking enabled.</span>'
        : '<strong>REFERENCE REQUIRED</strong><span>Raw encoder data is live. Set Zero or a known angle before it can drive camera tracking.</span>';
    }
    const diagnostics=card.querySelector('[data-runtime="encoder-diagnostics"]');
    if(diagnostics){const d=runtime.diagnostics||{},rotary=runtime.source==='linux_rotary',relative=runtime.relative_angle===null||runtime.relative_angle===undefined?'—':`${format(runtime.relative_angle,3)}°`,edgeAge=d.last_event_age_ms??d.last_edge_age_ms,lastEdge=edgeAge===null||edgeAge===undefined?'—':`${format(edgeAge,1)} ms`,referenceId=runtime.reference_id?escapeHtml(String(runtime.reference_id).slice(0,12)):'—',sourceDetails=rotary?`<div>Kernel driver</div><div>${escapeHtml(d.kernel_driver||'rotary-encoder')}</div><div>REL axis</div><div>${escapeHtml(d.rel_axis||'—')}</div><div>Device identity</div><div>${escapeHtml(d.device_identity||'—')}</div><div>Resolved device</div><div>${escapeHtml(d.resolved_event_device||'—')}</div><div>SYN_DROPPED</div><div>${d.evdev_dropped_events??0}</div>`:`<div>A state</div><div>${d.a_state??'—'}</div><div>B state</div><div>${d.b_state??'—'}</div><div>Requested GPIO event buffer</div><div>${d.kernel_event_buffer_size??'—'} (actual kernel capacity may be lower/capped)</div><div>Read batch size</div><div>${d.read_batch_size??'—'}</div><div>Confirmed global gaps</div><div>${d.confirmed_global_gaps??d.global_sequence_gaps??0}</div><div>Global-only gaps</div><div>${d.global_only_sequence_gaps??0}</div><div>Global out-of-order</div><div>${d.global_out_of_order_events??0}</div><div>Line sequence gaps</div><div>${d.line_sequence_gaps??0}</div><div>Illegal transitions</div><div>${d.illegal_transitions??0}</div>`;diagnostics.className='kv encoder-diagnostics';diagnostics.innerHTML=`${sourceDetails}<div>Raw count</div><div>${runtime.raw_count??'—'}</div><div>Reference count</div><div>${runtime.reference_count??'—'}</div><div>Relative angle</div><div>${relative}</div><div>Mapped angle</div><div>${runtime.value===null?'—':`${format(runtime.value,3)}°`}</div><div>Reference</div><div>${runtime.referenced?'REFERENCED':'REFERENCE REQUIRED'}</div><div>Reference angle</div><div>${runtime.referenced?`${format(runtime.reference_angle,3)}°`:'—'}</div><div>Reference ID</div><div title="${escapeHtml(runtime.reference_id||'')}">${referenceId}</div><div>Events/sec</div><div>${format(d.event_rate_hz??0,1)}</div><div>Peak events/sec</div><div>${format(d.peak_event_rate_hz??0,1)}</div><div>Total movement events</div><div>${d.events??0}</div><div>Counts/sec</div><div>${format(d.counts_per_second??0,1)}</div><div>Degrees/sec</div><div>${format(d.degrees_per_second??0,2)}°</div><div>Integrity-loss events</div><div>${d.integrity_loss_events??0}</div><div>Integrity</div><div>${d.integrity_status??'—'}</div><div>Last event age</div><div>${lastEdge}</div><div>Driver restarts</div><div>${d.restarts??0}</div><div>Driver status</div><div>${escapeHtml(runtime.status||'—')}</div><div>Last driver error</div><div>${escapeHtml(d.last_error||'—')}</div>`;}
    card.title=runtime.status||'';
  }
}
function renderSync(){const sync=state.sync,live=state.camera,render=state.render_camera,rows={'Acquisition target':'200 Hz','Measured history rate':sync.sample_rate_hz===null?'—':`${format(sync.sample_rate_hz,1)} Hz`,'Pose transport target':'50 Hz','Configured delay':`${sync.graphics_delay_ms} ms`,'History depth':`${sync.history_duration_seconds} s`,'Stored samples':sync.sample_count,'Oldest sample age':sync.oldest_age_ms===null?'—':`${format(sync.oldest_age_ms,1)} ms`,'Newest sample age':sync.newest_age_ms===null?'—':`${format(sync.newest_age_ms,1)} ms`,'Requested render age':`${sync.requested_render_age_ms} ms`,'Interpolation':sync.interpolation,'Live Pan / Tilt':`${format(live.pan,3)}° / ${format(live.tilt,3)}°`,'Render Pan / Tilt':`${format(render.pan,3)}° / ${format(render.tilt,3)}°`,'Effective difference':sync.effective_difference_ms===null?'—':`${format(sync.effective_difference_ms,1)} ms`};byId('syncDiagnostics').replaceChildren(...Object.entries(rows).flatMap(([key,value])=>{const label=document.createElement('div'),content=document.createElement('div');label.textContent=key;content.textContent=value;return[label,content];}));}

const simulatorIds=['pan','tilt','fov','height'];
function showSlider(id,value){byId(id+'Out').textContent=format(value,id==='height'?2:1)+(id==='height'?' m':id==='fov'?'°':' raw');}
function sourceLabel(source){return({quadrature_gpio:'GPIO',linux_rotary:'Linux Rotary Encoder',simulator:'Simulator',disabled:'Disabled',imu:'IMU',external:'External'})[source]||source;}
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
    gfxStatus.textContent=state.camera.world_valid?'CALIBRATED GFX':state.tracking.valid&&state.engineering.allow_provisional_gfx?'PROVISIONAL GFX ENABLED':state.tracking.valid?'STRICT GFX — WORLD CALIBRATION REQUIRED':'GFX BLOCKED — TRACKING INVALID';
    gfxStatus.className=state.camera.world_valid?'status-ok':state.tracking.valid&&state.engineering.allow_provisional_gfx?'status-warn':'status-bad';
    for(const id of ['previewSource','previewUrl','savePreviewMedia'])byId(id).disabled=false;
    if(byId('previewSource')!==document.activeElement)byId('previewSource').value=state.engineering.preview_source||'none';
    if(byId('previewUrl')!==document.activeElement)byId('previewUrl').value=state.engineering.preview_url||'';
    applying=false;
    byId('trackingStatus').textContent=state.tracking.valid?(state.camera.world_valid?'TRACKING + WORLD VALID':'TRACKING VALID · WORLD CALIBRATION REQUIRED'):'TRACKING INVALID';
    byId('trackingStatus').className=state.tracking.valid?(state.camera.world_valid?'status-ok':'status-warn'):'status-bad';
    if(event?.type!=='pose')renderProfiles();
    renderAxes();renderSync();
    const world=state.world_calibrations.active,solution=world.solution;
    byId('live').innerHTML=`<div>Profile</div><div>${escapeHtml(state.profiles.active.name)}</div><div>World calibration</div><div>${escapeHtml(world.name)} · ${state.camera.world_valid?'VALID':'NOT VALID'}</div><div>RMS</div><div>${solution?`${format(solution.rms_angular_error,4)}°`:'—'}</div><div>Camera XYZ</div><div>${format(state.camera.x,2)}, ${format(state.camera.y,2)}, ${format(state.camera.z,2)} m</div><div>Pan</div><div>${format(state.camera.pan,3)}°</div><div>Tilt</div><div>${format(state.camera.tilt,3)}°</div><div>FOV</div><div>${format(state.camera.fov,2)}°</div><div>Clients</div><div>${state.clients}</div>`;
    byId('debugState').textContent=`Debug: ${state.engineering.debug_enabled?'enabled':'disabled'}`;
    if(connectionMode==='websocket')setUiStatus('STATE CONNECTED');
    if(connectionMode==='polling')setUiStatus('POLLING FALLBACK','WebSocket unavailable; HTTP polling is active while retrying.');
    if(connectionMode==='connecting')setUiStatus('STATE RECEIVED · WEBSOCKET CONNECTING');
  } catch(error) {
    applying=false;
    setUiStatus('UI ERROR',String(error));
    logFrontend('engineering','state_handler_error',{error:String(error)});
    throw error;
  }
}

function connectionStatus(status,detail){
  if(status==='connecting'&&!state)setUiStatus('UI LOADING');
  if(status==='websocket-open'){connectionMode='websocket';setUiStatus(state?'STATE CONNECTED':'CONNECTED — WAITING FOR STATE');}
  if(status==='websocket-error'){connectionMode='retrying';setUiStatus('WEBSOCKET ERROR / RETRYING',detail);}
  if(status==='polling'){connectionMode='polling';setUiStatus('POLLING FALLBACK',detail||'WebSocket unavailable; HTTP polling is active while retrying.');}
  if(status==='state-error'&&!state)setUiStatus('STATE ERROR',detail);
  if(status==='state-received'&&state){
    if(connectionMode==='websocket')setUiStatus('STATE CONNECTED');
    if(connectionMode==='polling')setUiStatus('POLLING FALLBACK','WebSocket unavailable; HTTP polling is active while retrying.');
  }
}

connectState(renderState,'engineering',connectionStatus);
async function refreshSystem(){try{const system=await api('/api/system');byId('system').innerHTML=Object.entries(system).map(([key,value])=>`<div>${escapeHtml(key)}</div><div>${escapeHtml(value)}</div>`).join('');}catch(error){byId('system').textContent=`System information unavailable: ${error}`;logFrontend('engineering','system_fetch_error',{error:String(error)});}}refreshSystem();setInterval(refreshSystem,5000);
