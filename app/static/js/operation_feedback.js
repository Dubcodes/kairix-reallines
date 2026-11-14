export async function runOperationFeedback({controls=[],statusNode,errorNode=null,operation,confirmed='Confirmed',keepDisabled=false}){
  controls.forEach(control=>control.disabled=true);
  if(errorNode)errorNode.textContent='';
  statusNode.textContent='Processing';statusNode.className='status-warn';
  try{
    const result=await operation();
    statusNode.textContent=confirmed;statusNode.className='status-ok';
    if(!keepDisabled)controls.forEach(control=>control.disabled=false);
    return{ok:true,result};
  }catch(error){
    statusNode.textContent='Failed';statusNode.className='status-bad';
    if(errorNode)errorNode.textContent=String(error);
    controls.forEach(control=>control.disabled=false);
    return{ok:false,error};
  }
}
