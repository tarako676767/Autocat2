'use strict';

const config=window.CHAT_ADMIN_CONFIG||{};
const list=document.getElementById('restrictions');
const form=document.getElementById('code-form');
const codeInput=document.getElementById('unlock-code');
const statusEl=document.getElementById('status');

async function api(url,body={}){
  const response=await fetch(url,{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.error||`通信エラー (${response.status})`);
  return data;
}

function setStatus(message,type=''){
  statusEl.textContent=message||'';statusEl.className=`status ${type}`;
}

function restrictionNode(item){
  const row=document.createElement('div');row.className='restriction';
  const body=document.createElement('div');
  const name=document.createElement('div');name.className='name';name.textContent=`${item.target_name} / ${item.target_id}`;body.appendChild(name);
  const meta=document.createElement('div');meta.className='meta';
  if(item.type==='ban')meta.textContent='永久BAN';
  else meta.textContent=`タイムアウト期限: ${new Date(item.expires_at*1000).toLocaleString('ja-JP')}`;
  body.appendChild(meta);row.appendChild(body);
  const clear=document.createElement('button');clear.type='button';clear.className='btn';clear.textContent='制限解除';
  clear.addEventListener('click',async()=>{if(!confirm(`${item.target_name} の制限を解除しますか？`))return;try{await api('/api/chat/admin/clear-restriction',{token:item.token});setStatus('制限を解除しました。','success');await load();}catch(error){setStatus(error.message,'error');}});
  row.appendChild(clear);return row;
}

async function load(){
  try{
    const data=await api('/api/chat/admin/state');
    const fragment=document.createDocumentFragment();
    if(!(data.restrictions||[]).length){const empty=document.createElement('div');empty.className='empty';empty.textContent='現在制限中のユーザーはいません。';fragment.appendChild(empty);}
    else data.restrictions.forEach(item=>fragment.appendChild(restrictionNode(item)));
    list.replaceChildren(fragment);
  }catch(error){list.textContent=error.message;list.className='error';}
}

form.addEventListener('submit',async event=>{
  event.preventDefault();setStatus('');
  try{const data=await api('/api/chat/admin/unban-code',{code:codeInput.value});setStatus(`${data.target_name} のBANを解除しました。`,'success');codeInput.value='';await load();}
  catch(error){setStatus(error.message,'error');}
});

load();
