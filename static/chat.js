'use strict';

const config=window.CHAT_CONFIG||{};
const state={room:'free',fingerprint:'',actor:null,restriction:null,dmTarget:'',dmTargetInfo:null,loading:false,timer:null};
const messagesEl=document.getElementById('messages');
const composer=document.getElementById('composer');
const input=document.getElementById('message-input');
const sendButton=document.getElementById('send-button');
const readonly=document.getElementById('readonly');
const restrictionEl=document.getElementById('restriction');
const identityEl=document.getElementById('identity');
const notificationNotice=document.getElementById('notification-notice');
const notificationButton=document.getElementById('notification-button');
const adminPostOptions=document.getElementById('admin-post-options');
const notifyMode=document.getElementById('notify-mode');
const toast=document.getElementById('toast');

function showToast(message,type=''){
  toast.textContent=String(message||'');toast.className=`toast show ${type}`;
  clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>{toast.className='toast';},3500);
}

function fallbackHash(value){
  const seeds=[0x811c9dc5,0x9e3779b9,0x85ebca6b,0xc2b2ae35,0x27d4eb2f,0x165667b1,0xd3a2646c,0xfd7046c5];
  return seeds.map(seed=>{let h=seed>>>0;for(let i=0;i<value.length;i++){h^=value.charCodeAt(i);h=Math.imul(h,16777619)>>>0;}return h.toString(16).padStart(8,'0');}).join('');
}

async function browserFingerprint(){
  let installId='';
  try{
    installId=localStorage.getItem('autocat_install_id_v1')||'';
    if(!/^[0-9a-f]{32}$/.test(installId)){
      const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
      installId=[...bytes].map(v=>v.toString(16).padStart(2,'0')).join('');
      localStorage.setItem('autocat_install_id_v1',installId);
    }
  }catch{installId='storage-unavailable';}
  let canvas='';
  try{const c=document.createElement('canvas');c.width=220;c.height=45;const x=c.getContext('2d');x.font='15px Arial';x.fillStyle='#38bdf8';x.fillText('AUTOCAT JP CHAT',4,20);canvas=c.toDataURL();}catch{}
  const signals=[installId,navigator.userAgent,navigator.platform,navigator.language,(navigator.languages||[]).join(','),navigator.hardwareConcurrency||0,navigator.deviceMemory||0,navigator.maxTouchPoints||0,screen.width||0,screen.height||0,screen.colorDepth||0,Intl.DateTimeFormat().resolvedOptions().timeZone||'',canvas].join('||');
  if(crypto.subtle&&window.TextEncoder){const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(signals));return [...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('');}
  return fallbackHash(signals);
}

async function api(url,options={}){
  const response=await fetch(url,{cache:'no-store',credentials:'same-origin',...options});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.error||`通信エラー (${response.status})`);
  return data;
}

function formatTime(timestamp){
  return new Intl.DateTimeFormat('ja-JP',{hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(Number(timestamp)*1000));
}

function avatarNode(item){
  const wrap=document.createElement('div');wrap.className='avatar';
  if(item.author_avatar||item.avatar){const img=document.createElement('img');img.src=item.author_avatar||item.avatar;img.alt='';img.referrerPolicy='no-referrer';wrap.appendChild(img);}
  else{wrap.textContent=String(item.author_name||item.name||'?').slice(0,2);}
  return wrap;
}

function actionButton(label,handler){
  const button=document.createElement('button');button.type='button';button.className='delete';button.textContent=label;button.addEventListener('click',handler);return button;
}

function messageNode(message){
  const row=document.createElement('article');row.className='message';row.dataset.id=String(message.id);
  row.appendChild(avatarNode(message));
  const body=document.createElement('div');
  const meta=document.createElement('div');meta.className='meta';
  const name=document.createElement('span');name.className='name';name.textContent=message.author_name||message.author_id;meta.appendChild(name);
  const id=document.createElement('span');id.className='id';id.textContent=message.author_id;meta.appendChild(id);
  if(message.is_admin){const badge=document.createElement('span');badge.className='admin-badge';badge.textContent='運営';meta.appendChild(badge);}
  const at=document.createElement('span');at.className='time';at.textContent=formatTime(message.created_at);meta.appendChild(at);body.appendChild(meta);
  const content=document.createElement('div');content.className=`content${message.deleted?' deleted':''}`;content.textContent=message.deleted?'このメッセージは削除されました':message.content;body.appendChild(content);row.appendChild(body);
  const actions=document.createElement('div');actions.className='actions';
  if(message.can_delete&&!message.deleted)actions.appendChild(actionButton('削除',()=>deleteMessage(message.id)));
  if(message.can_dm&&!message.deleted)actions.appendChild(actionButton('DM',()=>openDM(message.dm_token)));
  if(message.can_moderate){
    if(message.restriction_type)actions.appendChild(actionButton('制限解除',()=>moderate(message.id,'unban')));
    else{
      actions.appendChild(actionButton('タイムアウト',()=>timeoutUser(message.id)));
      actions.appendChild(actionButton('BAN',()=>moderate(message.id,'ban')));
    }
  }
  row.appendChild(actions);return row;
}

function renderMessages(messages){
  const nearBottom=messagesEl.scrollHeight-messagesEl.scrollTop-messagesEl.clientHeight<100;
  const fragment=document.createDocumentFragment();
  if(state.room==='dm'&&state.dmTargetInfo){
    const head=document.createElement('div');head.className='dm-head';
    head.appendChild(actionButton('← DM一覧',()=>{state.dmTarget='';state.dmTargetInfo=null;history.replaceState(null,'','/chat?room=dm');loadState();}));
    const title=document.createElement('strong');title.textContent=`${state.dmTargetInfo.name} / ${state.dmTargetInfo.id}`;head.appendChild(title);fragment.appendChild(head);
  }
  if(!messages.length){const empty=document.createElement('div');empty.className='empty';empty.textContent=state.room==='admin'?'運営からのお知らせはまだありません。':state.room==='dm'?'DMはまだありません。':'まだメッセージがありません。';fragment.appendChild(empty);}
  else messages.forEach(message=>fragment.appendChild(messageNode(message)));
  messagesEl.replaceChildren(fragment);
  if(nearBottom)messagesEl.scrollTop=messagesEl.scrollHeight;
}

function renderConversations(conversations){
  const fragment=document.createDocumentFragment();
  if(!conversations.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='公開チャットの投稿者にある「DM」から会話を開始できます。';fragment.appendChild(empty);}
  for(const item of conversations){
    const button=document.createElement('button');button.type='button';button.className='conversation';button.appendChild(avatarNode(item));
    const body=document.createElement('div');const name=document.createElement('div');name.className='name';name.textContent=`${item.name} / ${item.id}`;body.appendChild(name);
    const preview=document.createElement('div');preview.className='preview';preview.textContent=item.last_content||'';body.appendChild(preview);button.appendChild(body);
    const time=document.createElement('span');time.className='time';time.textContent=formatTime(item.last_created_at);button.appendChild(time);
    button.addEventListener('click',()=>openDM(item.dm_token));fragment.appendChild(button);
  }
  messagesEl.replaceChildren(fragment);
}

function updateRestriction(){
  restrictionEl.replaceChildren();
  if(!state.restriction){restrictionEl.classList.remove('show');return;}
  restrictionEl.classList.add('show');
  const text=document.createElement('div');
  if(state.restriction.type==='ban')text.textContent='このIDはBANされています。管理者へ次のBAN解除コードを伝えてください。';
  else text.textContent=`このIDは ${new Date(state.restriction.expires_at*1000).toLocaleString('ja-JP')} までタイムアウト中です。`;
  restrictionEl.appendChild(text);
  if(state.restriction.unlock_code){const code=document.createElement('span');code.className='unlock-code';code.textContent=state.restriction.unlock_code;restrictionEl.appendChild(code);}
}

function updateRoomPermissions(){
  const adminReadonly=state.room==='admin'&&!state.actor?.is_admin;
  const dmNoTarget=state.room==='dm'&&!state.dmTarget;
  const restricted=Boolean(state.restriction);
  const cannotPost=adminReadonly||dmNoTarget||restricted;
  readonly.style.display=cannotPost?'block':'none';composer.style.display=cannotPost?'none':'grid';
  if(restricted)readonly.textContent=state.restriction.type==='ban'?'BAN中のため投稿できません。':'タイムアウト中のため投稿できません。';
  else if(adminReadonly)readonly.textContent=config.discordLoggedIn?'管理者チャットは運営のみ投稿できます。':'管理者チャットへ投稿するにはDiscordログインしてください。';
  else if(dmNoTarget)readonly.textContent='DM相手を選択してください。';
  adminPostOptions.classList.toggle('show',state.room==='admin'&&Boolean(state.actor?.is_admin)&&!restricted);
  document.querySelectorAll('.tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.room===state.room));
  updateRestriction();
}

async function loadState(silent=false){
  if(state.loading)return;state.loading=true;
  try{
    let data;
    if(state.room==='dm'){
      data=await api('/api/chat/dm/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:state.fingerprint,target_token:state.dmTarget})});
      state.actor=data.actor;state.restriction=data.restriction||null;state.dmTargetInfo=data.target||null;
      if(state.dmTarget)renderMessages(data.messages||[]);else renderConversations(data.conversations||[]);
    }else{
      data=await api('/api/chat/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:state.fingerprint,room:state.room,after_id:0})});
      state.actor=data.actor;state.restriction=data.restriction||null;renderMessages(data.messages||[]);
    }
    identityEl.textContent=state.actor.discord?`${state.actor.name} / ${state.actor.id}`:state.actor.id;
    updateRoomPermissions();
  }catch(error){if(!silent)showToast(error.message,'error');}
  finally{state.loading=false;}
}

async function sendMessage(event){
  event.preventDefault();const content=input.value.trim();if(!content)return;
  sendButton.disabled=true;
  try{
    if(state.room==='dm'){
      await api('/api/chat/dm/messages',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({fingerprint:state.fingerprint,target_token:state.dmTarget,content})});
    }else{
      const notify=state.room==='admin'&&notifyMode.value==='on';
      await api('/api/chat/messages',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({fingerprint:state.fingerprint,room:state.room,content,notify})});
    }
    input.value='';await loadState(true);messagesEl.scrollTop=messagesEl.scrollHeight;
  }catch(error){showToast(error.message,'error');}
  finally{sendButton.disabled=false;input.focus();}
}

async function deleteMessage(id){
  if(!confirm('このメッセージを削除しますか？'))return;
  const url=state.room==='dm'?`/api/chat/dm/messages/${Number(id)}`:`/api/chat/messages/${Number(id)}`;
  try{await api(url,{method:'DELETE',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({fingerprint:state.fingerprint})});await loadState(true);}
  catch(error){showToast(error.message,'error');}
}

async function moderate(id,action,durationSeconds=null){
  const label=action==='ban'?'BAN':action==='unban'?'制限解除':'タイムアウト';
  if(!confirm(`この利用者を${label}しますか？`))return;
  try{
    await api(`/api/chat/messages/${Number(id)}/moderation`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({fingerprint:state.fingerprint,action,duration_seconds:durationSeconds})});
    showToast(`${label}しました。`);await loadState(true);
  }catch(error){showToast(error.message,'error');}
}

function timeoutUser(id){
  const selected=prompt('タイムアウト時間を選択してください。\n10 = 10分\n60 = 1時間\n1440 = 1日\n10080 = 7日','60');
  if(selected===null)return;
  const durations={10:600,60:3600,1440:86400,10080:604800};
  const seconds=durations[Number(selected)];
  if(!seconds){showToast('10・60・1440・10080のどれかを入力してください。','error');return;}
  moderate(id,'timeout',seconds);
}

function openDM(token){
  state.room='dm';state.dmTarget=String(token||'');state.dmTargetInfo=null;
  history.replaceState(null,'',`/chat?room=dm&target=${encodeURIComponent(state.dmTarget)}`);
  updateRoomPermissions();messagesEl.innerHTML='<div class="empty">DMを読み込み中...</div>';loadState();
}

function base64UrlToUint8Array(value){
  const padding='='.repeat((4-value.length%4)%4);const raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from([...raw].map(ch=>ch.charCodeAt(0)));
}

async function registerPush(promptUser=false){
  if(!('serviceWorker'in navigator)){if(promptUser)throw new Error('このブラウザは通知に対応していません。');return;}
  if(!('PushManager'in window)||!('Notification'in window)||!config.vapidPublicKey){if(promptUser)throw new Error('通知機能を利用できません。');return;}
  let permission=Notification.permission;
  if(promptUser&&permission==='default')permission=await Notification.requestPermission();
  if(permission!=='granted'){if(promptUser)throw new Error('通知が許可されませんでした。');return;}
  const registration=await navigator.serviceWorker.register('/chat-sw.js',{scope:'/chat'});
  let subscription=await registration.pushManager.getSubscription();
  if(!subscription)subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64UrlToUint8Array(config.vapidPublicKey)});
  await api('/api/chat/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({fingerprint:state.fingerprint,subscription:subscription.toJSON()})});
  notificationNotice.classList.remove('show');if(promptUser)showToast('運営通知をオンにしました。');
}

function setupNotifications(){
  if(!('Notification'in window)||!config.vapidPublicKey)return;
  if(Notification.permission==='default')notificationNotice.classList.add('show');
  else if(Notification.permission==='granted')registerPush(false).catch(()=>{});
  notificationButton.addEventListener('click',()=>registerPush(true).catch(error=>showToast(error.message,'error')));
}

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',async()=>{
  state.room=tab.dataset.room;if(state.room!=='dm'){state.dmTarget='';state.dmTargetInfo=null;}
  history.replaceState(null,'',`/chat?room=${state.room}`);updateRoomPermissions();messagesEl.innerHTML='<div class="empty">読み込み中...</div>';await loadState();
}));
composer.addEventListener('submit',sendMessage);
input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();composer.requestSubmit();}});

(async()=>{
  const params=new URLSearchParams(location.search);const requested=params.get('room');state.room=['free','admin','dm'].includes(requested)?requested:'free';state.dmTarget=state.room==='dm'?(params.get('target')||''):'';
  state.fingerprint=await browserFingerprint();updateRoomPermissions();await loadState();setupNotifications();
  state.timer=setInterval(()=>loadState(true),2500);
})();
