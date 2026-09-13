'use strict';

const config=window.VIP_PURCHASE_CONFIG||{};
const state={key:String(config.selectedKey||''),ticket:null,timer:null,loading:false,selectedPlan:Number(config.selectedPlan)||30};
const plans=new Map((config.plans||[]).map(item=>[Number(item.days),item]));
const labels={pending:'申請受付',awaiting_payment:'支払い待ち',payment_review:'支払い確認中',completed:'VIP付与完了',cancelled:'キャンセル'};
const stepOrder=['pending','awaiting_payment','payment_review','completed'];
const messagesEl=document.getElementById('messages');
const composer=document.getElementById('composer');
const composerWrap=document.getElementById('composer-wrap');
const input=document.getElementById('input');
const send=document.getElementById('send');
const confirmEl=document.getElementById('confirm');
const confirmButton=document.getElementById('confirm-request');
const confirmError=document.getElementById('confirm-error');
const notifyButton=document.getElementById('notify');
const closeButton=document.getElementById('close-dm');
const newRequestTop=document.getElementById('new-request-top');
const quickActions=document.getElementById('quick-actions');
const paypayBox=document.getElementById('paypay-box');
const paypayForm=document.getElementById('paypay-form');
const paypayLink=document.getElementById('paypay-link');
const sidebar=document.getElementById('sidebar');
const sidebarBackdrop=document.getElementById('sidebar-backdrop');

async function api(url,options={}){const response=await fetch(url,{cache:'no-store',credentials:'same-origin',...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||`通信エラー (${response.status})`);return data;}
function toast(message){const el=document.getElementById('toast');el.textContent=message;el.className='toast show';clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.className='toast',2700);}
function fmt(timestamp){return new Date(Number(timestamp)*1000).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function yen(value){return `${Number(value||0).toLocaleString('ja-JP')}円`;}
function openPlans(){confirmError.textContent='';confirmEl.classList.add('show');closeSidebar();}
function closeSidebar(){sidebar.classList.remove('open');sidebarBackdrop.classList.remove('show');}

function updatePlanSelection(){const checked=document.querySelector('input[name="vip-plan"]:checked');state.selectedPlan=Number(checked?.value)||30;const plan=plans.get(state.selectedPlan);document.getElementById('confirm-total').textContent=yen(plan?.price_yen);}
document.querySelectorAll('input[name="vip-plan"]').forEach(item=>item.addEventListener('change',updatePlanSelection));

function renderTicketList(tickets){
  const root=document.getElementById('ticket-list');
  if(!tickets.length){root.innerHTML='<div class="empty" style="height:auto;padding:20px 4px">購入申請はまだありません。</div>';return;}
  const fragment=document.createDocumentFragment();
  for(const ticket of tickets){
    const button=document.createElement('button');button.type='button';button.className=`ticket-link${ticket.purchase_key===state.key?' active':''}`;
    const key=document.createElement('div');key.className='ticket-key';key.textContent=ticket.purchase_key;
    const meta=document.createElement('div');meta.className='ticket-meta';
    const status=document.createElement('span');status.textContent=`${ticket.requested_days}日・${labels[ticket.status]||ticket.status}`;
    const time=document.createElement('span');time.textContent=fmt(ticket.updated_at);meta.append(status,time);
    if(Number(ticket.user_unread)>0){const unread=document.createElement('span');unread.className='unread';unread.textContent=ticket.user_unread;meta.appendChild(unread);}
    button.append(key,meta);button.addEventListener('click',async()=>{state.key=ticket.purchase_key;history.replaceState(null,'',`/vip/purchase?key=${encodeURIComponent(state.key)}`);closeSidebar();await load(false);});fragment.appendChild(button);
  }
  root.replaceChildren(fragment);
}

function renderProgress(ticket){
  const progress=document.getElementById('progress');progress.hidden=false;progress.classList.toggle('cancelled',ticket.status==='cancelled');
  const currentIndex=Math.max(0,stepOrder.indexOf(ticket.status));
  progress.querySelectorAll('.progress-step').forEach((item,index)=>{item.classList.toggle('done',ticket.status==='completed'||(ticket.status!=='cancelled'&&index<currentIndex));item.classList.toggle('current',ticket.status!=='cancelled'&&index===currentIndex);});
}

function renderMessages(ticket){
  const nearBottom=messagesEl.scrollHeight-messagesEl.scrollTop-messagesEl.clientHeight<120;
  const fragment=document.createDocumentFragment();
  for(const item of ticket.messages||[]){const row=document.createElement('article');row.className=`message ${item.sender_role}`;const senderEl=document.createElement('div');senderEl.className='sender';senderEl.textContent=`${item.sender_label}・${fmt(item.created_at)}`;const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=item.content;row.append(senderEl,bubble);fragment.appendChild(row);}
  if(!(ticket.messages||[]).length){const empty=document.createElement('div');empty.className='empty';empty.textContent='メッセージはまだありません。';fragment.appendChild(empty);}
  messagesEl.replaceChildren(fragment);if(nearBottom)messagesEl.scrollTop=messagesEl.scrollHeight;
}

function render(ticket){
  state.ticket=ticket||null;
  const old=document.getElementById('chat-closed-note');if(old)old.remove();
  if(!ticket){state.key='';document.getElementById('order-summary').hidden=true;document.getElementById('progress').hidden=true;quickActions.hidden=true;paypayBox.hidden=true;composerWrap.hidden=true;closeButton.hidden=true;newRequestTop.hidden=false;document.getElementById('ticket-status').textContent='申請前';document.getElementById('ticket-meta').textContent='購入プランを選択してください';messagesEl.innerHTML='<div class="empty">購入プランを選択すると管理者とのDMが始まります。</div>';return;}
  state.key=ticket.purchase_key;
  document.getElementById('ticket-meta').textContent=`購入KEY ${ticket.purchase_key}`;
  document.getElementById('ticket-status').textContent=labels[ticket.status]||ticket.status;
  document.getElementById('order-summary').hidden=false;
  document.getElementById('summary-plan').textContent=`VIP ${ticket.requested_days}日`;
  document.getElementById('summary-price').textContent=yen(ticket.price_yen);
  document.getElementById('summary-account').textContent=ticket.account_public_id;
  document.getElementById('summary-key').textContent=ticket.purchase_key;
  renderProgress(ticket);renderMessages(ticket);
  const chatClosed=Boolean(ticket.chat_closed);const canSendPayPay=!chatClosed&&!ticket.payment_link&&['pending','awaiting_payment'].includes(ticket.status);closeButton.hidden=false;newRequestTop.hidden=!(chatClosed||['completed','cancelled'].includes(ticket.status));quickActions.hidden=chatClosed;composerWrap.hidden=chatClosed;paypayBox.hidden=!canSendPayPay;
  document.getElementById('show-paypay').hidden=!canSendPayPay;document.getElementById('paypay-sent').hidden=!ticket.payment_link;
  if(chatClosed){const note=document.createElement('div');note.id='chat-closed-note';note.className='chat-closed';note.textContent='この購入DMは閉じられています。履歴は確認できます。';messagesEl.before(note);document.getElementById('ticket-status').textContent=`${labels[ticket.status]||ticket.status}・DM終了`;}
}

async function load(silent=false){if(state.loading)return;state.loading=true;try{const query=state.key?`?key=${encodeURIComponent(state.key)}`:'';const data=await api(`/api/vip/purchase/state${query}`);renderTicketList(data.tickets||[]);render(data.ticket);}catch(error){if(!silent)toast(error.message);}finally{state.loading=false;}}

confirmButton.addEventListener('click',async()=>{updatePlanSelection();confirmButton.disabled=true;confirmError.textContent='';try{const data=await api('/api/vip/purchase/confirm',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({duration_days:state.selectedPlan})});state.key=data.ticket.purchase_key;history.replaceState(null,'',`/vip/purchase?key=${encodeURIComponent(state.key)}`);confirmEl.classList.remove('show');await load();toast(data.created?'購入申請を送信しました。':'進行中の購入申請を開きました。');}catch(error){confirmError.textContent=error.message;}finally{confirmButton.disabled=false;}});
document.getElementById('cancel-confirm').addEventListener('click',()=>{if(state.key)confirmEl.classList.remove('show');else location.assign('/vip-guide');});
document.getElementById('new-request').addEventListener('click',openPlans);newRequestTop.addEventListener('click',openPlans);
document.getElementById('history-toggle').addEventListener('click',()=>{sidebar.classList.add('open');sidebarBackdrop.classList.add('show');});sidebarBackdrop.addEventListener('click',closeSidebar);

async function sendText(content){if(!content||!state.key)return;send.disabled=true;try{await api('/api/vip/purchase/messages',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key,content})});input.value='';await load(true);messagesEl.scrollTop=messagesEl.scrollHeight;}catch(error){toast(error.message);}finally{send.disabled=false;}}
composer.addEventListener('submit',event=>{event.preventDefault();sendText(input.value.trim());});
document.getElementById('show-paypay').addEventListener('click',()=>{paypayBox.hidden=false;paypayLink.focus();});
document.getElementById('ask-question').addEventListener('click',()=>{input.focus();input.placeholder='購入について質問を入力してください';});
paypayForm.addEventListener('submit',async event=>{event.preventDefault();if(!state.key)return;const button=document.getElementById('send-paypay');button.disabled=true;try{const data=await api('/api/vip/purchase/paypay-link',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key,payment_link:paypayLink.value.trim()})});paypayLink.value='';await load();toast(data.changed?'PayPayリンクを管理者へ送りました。':'同じリンクは送信済みです。');}catch(error){toast(error.message);}finally{button.disabled=false;}});
closeButton.addEventListener('click',async()=>{if(!state.key||!confirm('この購入DMを閉じますか？\n\n利用者の一覧から削除されます。会話ログは管理者側の「削除済み」に保存されます。VIP契約自体は削除されません。'))return;closeButton.disabled=true;try{await api('/api/vip/purchase/close',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key})});state.key='';history.replaceState(null,'','/vip/purchase');await load();toast('購入チケットを削除しました。');}catch(error){toast(error.message);}finally{closeButton.disabled=false;}});

function base64UrlToUint8Array(value){const padding='='.repeat((4-value.length%4)%4);const raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from([...raw].map(ch=>ch.charCodeAt(0)));}
async function notificationState(){if(!('serviceWorker'in navigator)||!('PushManager'in window)||!('Notification'in window)||!config.vapidPublicKey){notifyButton.disabled=true;notifyButton.textContent='通知非対応';return;}const registration=await navigator.serviceWorker.register('/account-sw.js',{scope:'/'});const subscription=await registration.pushManager.getSubscription();if(subscription)await api('/api/account/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({subscription:subscription.toJSON()})});notifyButton.classList.toggle('on',Boolean(subscription));notifyButton.textContent=subscription?'通知オン':'通知をオン';return {registration,subscription};}
notifyButton.addEventListener('click',async()=>{try{const current=await notificationState();if(!current)return;if(current.subscription){await api('/api/account/push/subscribe',{method:'DELETE',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({endpoint:current.subscription.endpoint})});await current.subscription.unsubscribe();toast('DM通知をオフにしました。');}else{const permission=await Notification.requestPermission();if(permission!=='granted')throw new Error('通知が許可されませんでした。');const subscription=await current.registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64UrlToUint8Array(config.vapidPublicKey)});await api('/api/account/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({subscription:subscription.toJSON()})});toast('DM通知をオンにしました。');}await notificationState();}catch(error){toast(error.message);}});

updatePlanSelection();load();notificationState().catch(()=>{});state.timer=setInterval(()=>load(true),2500);
