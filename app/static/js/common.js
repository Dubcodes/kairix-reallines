import {poseIdentity,recordIncomingPose,setBrowserTransport} from './browser_metrics.js?v=world-media1';
import {shouldApplyOverlayRevision} from './dot_lock.js?v=world-media1';
import {mergeOrderedFullState,mergeOrderedPose} from './pose_order.js?v=world-media1';

export async function api(path, options={}) {
  const init = { ...options };
  init.headers = { 'Content-Type':'application/json', ...(options.headers || {}) };
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  const type = r.headers.get('content-type') || '';
  return type.includes('application/json') ? r.json() : r;
}

export function gateControlsUntilState(root=document) {
  const controls = [...root.querySelectorAll('button, input, select')];
  const previous = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  return () => controls.forEach((control, index) => { control.disabled = previous[index]; });
}

export function connectState(onState, page='unknown', onStatus=()=>{}) {
  let ws;
  let reconnectTimer;
  let statePollTimer;
  let posePollTimer;
  let stopped = false;
  let polling = false;
  let currentState = null;
  const status = (name, detail='') => {
    try { onStatus(name, detail); } catch (e) { console.error(e); }
  };
  const deliver = (data, type='state') => {
    currentState = data;
    if(type!=='calibration_overlay')recordIncomingPose(page,poseIdentity(data),performance.now(),data?.transport||null,type==='pose'?data?.server_mono:null);
    try {
      onState(data, {type});
      return true;
    } catch (e) {
      console.error(e);
      logFrontend(page, page === 'control' ? 'control_initialization_failed' : 'state_handler_error', {error:String(e)});
      return false;
    }
  };
  const deliverPose = (pose) => {
    if (!currentState) return;
    const merged=mergeOrderedPose(currentState,pose);
    if(!merged.applied)return;
    currentState=merged.state;
    deliver(currentState, 'pose');
  };
  const deliverCalibrationOverlay = (overlay) => {
    if (!currentState) return;
    if (!shouldApplyOverlayRevision(currentState.calibration_overlay, overlay)) return;
    currentState = {...currentState, calibration_overlay:overlay};
    deliver(currentState, 'calibration_overlay');
  };
  const fetchState = async () => {
    try {
      const response = await fetch('/api/state', {cache:'no-store'});
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      const incoming=await response.json();
      if (deliver(mergeOrderedFullState(currentState,incoming), 'state')) status('state-received');
    } catch (e) {
      console.error(e);
      logFrontend(page, 'state_fetch_error', {error:String(e)});
      status('state-error', String(e));
    }
  };
  const fetchPose = async () => {
    try {
      const response = await fetch('/api/pose', {cache:'no-store'});
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      deliverPose(await response.json());
    } catch (e) {
      console.error(e);
    }
  };
  const startPolling = (detail='WebSocket unavailable; retrying in the background') => {
    if (stopped) return;
    if (polling) {
      status('polling', detail);
      return;
    }
    polling = true;
    setBrowserTransport(page,'polling');
    status('polling', detail);
    const pollState = async () => {
      if (!polling || stopped) return;
      await fetchState();
      statePollTimer = setTimeout(pollState, 2000);
    };
    const pollPose = async () => {
      if (!polling || stopped) return;
      await fetchPose();
      posePollTimer = setTimeout(pollPose, 50);
    };
    pollState();
    pollPose();
  };
  const stopPolling = () => {
    polling = false;
    clearTimeout(statePollTimer);
    clearTimeout(posePollTimer);
  };
  const connect = () => {
    if (stopped) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => {
      stopPolling();
      setBrowserTransport(page,'websocket');
      status('websocket-open');
      logFrontend(page, 'ws_open', {});
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'state') deliver(mergeOrderedFullState(currentState,msg.data), 'state');
        if (msg.type === 'pose') deliverPose(msg.data);
        if (msg.type === 'calibration_overlay') deliverCalibrationOverlay(msg.data);
      } catch (e) {
        console.error(e);
        logFrontend(page, page === 'control' ? 'control_initialization_failed' : 'ws_message_error', {error:String(e)});
      }
    };
    ws.onerror = () => {
      status('websocket-error', 'WebSocket connection failed; HTTP polling will remain available.');
      logFrontend(page, 'ws_error', {});
    };
    ws.onclose = (event) => {
      const detail = `WebSocket closed (${event.code}${event.reason ? `: ${event.reason}` : ''}); retrying in 5 seconds.`;
      status('websocket-error', detail);
      startPolling(detail);
      logFrontend(page, 'ws_close', {code:event.code, reason:event.reason, clean:event.wasClean});
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 5000);
    };
  };
  status('connecting');
  fetchState();
  connect();
  return () => {
    stopped = true;
    clearTimeout(reconnectTimer);
    stopPolling();
    if (ws) ws.close();
  };
}

export function logFrontend(page, event, data={}) {
  fetch('/api/debug/frontend', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({page,event,data})
  }).catch(()=>{});
}

window.addEventListener('error', (e) => logFrontend(document.body.dataset.page || 'unknown', 'window_error', {
  message:e.message, filename:e.filename, line:e.lineno, col:e.colno
}));
window.addEventListener('unhandledrejection', (e) => logFrontend(document.body.dataset.page || 'unknown', 'unhandled_rejection', {reason:String(e.reason)}));
