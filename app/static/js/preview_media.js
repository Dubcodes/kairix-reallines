export function validPreviewSettings(engineering){
  const source=engineering?.preview_source||'none',url=String(engineering?.preview_url||'').trim();
  return{source,url,valid:(source==='mjpeg'||source==='video')&&/^https?:\/\//i.test(url)};
}

export function updatePreviewMedia(root,engineering,enabled=true){
  const image=root.querySelector('[data-preview-image]'),video=root.querySelector('[data-preview-video]'),status=root.querySelector('[data-preview-status]'),settings=validPreviewSettings(engineering);
  const active=enabled&&settings.valid;
  if(!root.dataset.previewHandlers){
    const loaded=()=>{if(status)status.hidden=true;};
    const failed=()=>{if(status){status.hidden=false;status.textContent='CAMERA PREVIEW FAILED TO LOAD';}};
    image.addEventListener('load',loaded);image.addEventListener('error',failed);video.addEventListener('canplay',loaded);video.addEventListener('error',failed);root.dataset.previewHandlers='true';
  }
  image.hidden=!(active&&settings.source==='mjpeg');video.hidden=!(active&&settings.source==='video');
  if(!image.hidden&&image.src!==settings.url){if(status){status.hidden=false;status.textContent='LOADING CAMERA PREVIEW';}image.src=settings.url;}
  if(!video.hidden&&video.src!==settings.url){if(status){status.hidden=false;status.textContent='LOADING CAMERA PREVIEW';}video.src=settings.url;video.play().catch(()=>{});}
  if(image.hidden)image.removeAttribute('src');if(video.hidden){video.pause();video.removeAttribute('src');}
  if(status&&!active){status.hidden=false;status.textContent=!enabled?'MEDIA OFF':settings.source==='none'?'NO CAMERA PREVIEW SOURCE':'PREVIEW SOURCE URL REQUIRED';}
  root.classList.toggle('media-active',active);return active;
}
