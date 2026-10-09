export async function api(path, options={}) {
  const init = { ...options };
  init.headers = { 'Content-Type':'application/json', ...(options.headers || {}) };
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  const type = r.headers.get('content-type') || '';
  return type.includes('application/json') ? r.json() : r;
}

export function connectState(onState, page='unknown') {
  let ws;
  let reconnectTimer;
  let statePollTimer;
  let posePollTimer;
  let stopped = false;
  let polling = false;
  let currentState = null;
  const deliver = (data, type='state') => {
    currentState = data;
    try {
      onState(data, {type});
    } catch (e) {
      console.error(e);
      logFrontend(page, page === 'control' ? 'control_initialization_failed' : 'state_handler_error', {error:String(e)});
    }
  };
  const deliverPose = (pose) => {
    if (!currentState) return;
    currentState = {...currentState, ...pose, tracking:{...currentState.tracking, ...pose.tracking, axes:{...currentState.tracking.axes, ...(pose.tracking?.axes||{})}}};
    deliver(currentState, 'pose');
  };
  const fetchState = async () => {
    try {
      const response = await fetch('/api/state', {cache:'no-store'});
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      deliver(await response.json(), 'state');
    } catch (e) {
      console.error(e);
      logFrontend(page, 'state_fetch_error', {error:String(e)});
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
  const startPolling = () => {
    if (polling || stopped) return;
    polling = true;
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
      logFrontend(page, 'ws_open', {});
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'state') deliver(msg.data, 'state');
        if (msg.type === 'pose') deliverPose(msg.data);
      } catch (e) {
        console.error(e);
        logFrontend(page, page === 'control' ? 'control_initialization_failed' : 'ws_message_error', {error:String(e)});
      }
    };
    ws.onclose = () => {
      startPolling();
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 5000);
    };
  };
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
