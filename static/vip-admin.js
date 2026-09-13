'use strict';

const config=window.VIP_ADMIN_CONFIG||{};
const state={key:String(config.selectedKey||''),detailKey:'',filter:'',statusFilter:'unhandled',tabInitialized:false,data:null,loading:false,timer:null};
const labels={pending:'申請受付',awaiting_payment:'支払い待ち',payment_review:'支払い確認中',completed:'VIP付与完了',cancelled:'キャンセル'};
const statusEl=document.getElementById('status');
const detailEl=document.getElementById('detail');
const notifyButton=document.getElementById('notify');

async function api(url,options={}){const response=await fetch(url,{cache:'no-store',credentials:'same-origin',...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||`通信エラー (${response.status})`);return data;}
function fmt(timestamp){return timestamp?new Date(Number(timestamp)*1000).toLocaleString('ja-JP'):'無期限';}
function yen(value){return `${Number(value||0).toLocaleString('ja-JP')}円`;}
function setStatus(message,error=false){statusEl.textContent=message;statusEl.className=`status${error?' error':''}`;clearTimeout(setStatus.timer);setStatus.timer=setTimeout(()=>{statusEl.textContent='';},4500);}
function includesFilter(...values){const query=state.filter;if(!query)return true;return values.some(value=>String(value||'').toLocaleLowerCase('ja').includes(query));}
function includesStatus(item){if(state.statusFilter==='deleted')return Boolean(item.deleted);if(state.statusFilter==='unhandled')return ['pending','awaiting_payment'].includes(item.status);if(state.statusFilter==='working')return item.status==='payment_review';if(state.statusFilter==='done')return ['completed','cancelled'].includes(item.status);return true;}
function statusGroup(value){const item=typeof value==='object'&&value?value:null;if(item?.deleted)return'deleted';const status=item?item.status:value;if(['pending','awaiting_payment'].includes(status))return'unhandled';if(status==='payment_review')return'working';return'done';}
function activateTicketTab(group){state.statusFilter=group;document.querySelectorAll('.tab').forEach(item=>item.classList.toggle('active',item.dataset.view==='tickets'&&item.dataset.status===group));document.querySelectorAll('.side-panels').forEach(panel=>panel.classList.toggle('active',panel.id==='tickets'));}

function renderTickets(source){
  if(state.statusFilter==='deleted')source=state.data?.deleted_tickets||[];
  const rows=[...source].filter(item=>includesStatus(item)&&includesFilter(item.username,item.purchase_key,item.account_public_id,item.requested_days,item.price_yen)).sort((a,b)=>Number(b.admin_unread)-Number(a.admin_unread));
  const root=document.getElementById('tickets');if(!rows.length){const emptyLabels={unhandled:'未対応の申請はありません。',working:'対応中の申請はありません。',done:'対応済みの申請はありません。',deleted:'削除済みのログはありません。'};root.innerHTML=`<div class="empty">${emptyLabels[state.statusFilter]||'一致する購入申請はありません。'}</div>`;return;}
  const fragment=document.createDocumentFragment();
  for(const item of rows){
    const button=document.createElement('button');button.type='button';button.className=`ticket${item.purchase_key===state.key?' active':''}`;button.innerHTML='<div class="ticket-top"><span class="ticket-name"></span><span class="badge"></span></div><div class="ticket-key"></div><div class="ticket-meta"><span></span><span></span><span></span></div>';
    button.querySelector('.ticket-name').textContent=item.username;button.querySelector('.badge').textContent=item.deleted?'削除済み':(labels[item.status]||item.status);button.querySelector('.ticket-key').textContent=item.purchase_key;
    const meta=button.querySelectorAll('.ticket-meta span');meta[0].textContent=`${item.requested_days}日・${yen(item.price_yen)}`;meta[1].textContent=`ID ${item.account_public_id}`;meta[2].textContent=fmt(item.deleted_at||item.updated_at);
    if(item.payment_link){meta[0].textContent+=`・PayPay受信済み`;}if(Number(item.admin_unread)>0){const unread=document.createElement('span');unread.className='unread';unread.textContent=item.admin_unread;button.querySelector('.ticket-top').appendChild(unread);}
    button.addEventListener('click',async()=>{state.key=item.purchase_key;history.replaceState(null,'',`/admin/vip?ticket=${encodeURIComponent(state.key)}`);await load(false,true);if(matchMedia('(max-width:850px)').matches)detailEl.scrollIntoView({behavior:'smooth',block:'start'});});fragment.appendChild(button);
  }
  root.replaceChildren(fragment);
}

function renderSubscribers(rows){rows=rows.filter(item=>includesFilter(item.username,item.public_id));const root=document.getElementById('subscribers');if(!rows.length){root.innerHTML='<div class="empty">一致するVIP契約者はいません。</div>';return;}const fragment=document.createDocumentFragment();for(const item of rows){const row=document.createElement('div');row.className='subscriber';const body=document.createElement('div');const name=document.createElement('strong');name.textContent=item.username;const id=document.createElement('code');id.textContent=item.public_id;const expiry=document.createElement('small');expiry.textContent=item.vip_expires_at?`期限 ${fmt(item.vip_expires_at)}`:'無期限';body.append(name,id,expiry);const revoke=document.createElement('button');revoke.className='btn danger';revoke.type='button';revoke.textContent='VIP剥奪';revoke.addEventListener('click',()=>revokeVip(item.public_id,item.username));row.append(body,revoke);fragment.appendChild(row);}root.replaceChildren(fragment);}
function renderAccounts(rows){
  rows=rows.filter(item=>includesFilter(item.username,item.public_id));
  const root=document.getElementById('accounts');
  if(!rows.length){root.innerHTML='<div class="empty">一致するサイトアカウントはありません。</div>';return;}
  const stateLabels={active:'利用中',suspended:'一時停止',banned:'BAN',gban:'GBAN',released:'旧・登録枠解放済み',deleted:'旧・削除済み'};
  const fragment=document.createDocumentFragment();
  for(const item of rows){
    const row=document.createElement('div');row.className='account-row';
    const body=document.createElement('div');
    const name=document.createElement('strong');name.textContent=item.username;
    const id=document.createElement('code');id.textContent=item.public_id;
    const meta=document.createElement('small');meta.textContent=`登録 ${fmt(item.created_at)}${item.is_vip?'・VIP':''}${item.deleted_at?`・削除 ${fmt(item.deleted_at)}`:''}`;
    const badge=document.createElement('span');badge.className=`account-state ${item.status}`;badge.textContent=stateLabels[item.status]||item.status;
    body.append(name,id,meta,badge);
    const actions=document.createElement('div');actions.className='account-actions';
    if(!item.deleted){
      actions.append(accountEditButton('名前変更',()=>renameAccount(item)));
      if(item.is_vip){actions.append(accountEditButton('VIP剥奪',()=>revokeVip(item.public_id,item.username),true));}
      else{actions.append(accountEditButton('VIP付与',()=>grantVipForAccount(item)));}
      actions.append(item.suspended_at?accountButton('再開','activate',item):accountButton('一時停止','suspend',item));
      actions.append(item.banned_at?accountButton('BAN解除','unban',item):accountButton('BAN','ban',item,true));
      actions.append(item.gban_active?accountButton('GBAN解除','ungban',item):accountButton('GBAN','gban',item,true));
    }
    row.append(body,actions);fragment.appendChild(row);
  }
  root.replaceChildren(fragment);
}
function accountButton(label,action,item,danger=false){const button=document.createElement('button');button.type='button';button.className=`btn${danger?' danger':''}`;button.textContent=label;button.addEventListener('click',()=>manageAccount(item.public_id,item.username,action));return button;}
function accountEditButton(label,handler,danger=false){const button=document.createElement('button');button.type='button';button.className=`btn${danger?' danger':''}`;button.textContent=label;button.addEventListener('click',handler);return button;}

function appendTemplateButton(root,label,text){const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=label;button.addEventListener('click',()=>{const textarea=detailEl.querySelector('.composer textarea');if(!textarea)return;textarea.value=text;textarea.focus();});root.appendChild(button);}

function renderDetail(ticket){
  state.detailKey=String(ticket?.purchase_key||'');if(!ticket){detailEl.innerHTML='<div class="empty">左の購入申請を選択してください。</div>';return;}state.key=ticket.purchase_key;
  document.getElementById('grant-id').value=ticket.account_public_id;document.getElementById('grant-key').value=ticket.purchase_key;document.getElementById('grant-days').value=String(ticket.requested_days);
  detailEl.innerHTML='<div class="detail-head"><div class="detail-title"><h2></h2><span class="badge"></span><button class="btn danger delete-ticket" type="button">DMを閉じる</button></div><div class="ids"><span>サイトID <code class="site-id"></code></span><span>購入KEY <code class="purchase-key"></code></span><span class="vip-state"></span><span class="chat-state"></span></div><details class="manual-status"><summary>状態を手動で変更</summary><div class="status-row"></div></details></div><div class="order-strip"><div><span>申請プラン</span><strong class="plan"></strong></div><div><span>支払い金額</span><strong class="price"></strong></div><div><span>PayPayリンク</span><strong class="payment"></strong></div></div><div class="next-action"><div><span>次にすること</span><strong></strong></div><div class="next-buttons"></div></div><div class="paypay-slot"></div><div class="quick-row"></div><div class="messages"></div><form class="composer"><textarea maxlength="500" placeholder="利用者へメッセージを送る"></textarea><button class="btn primary" type="submit">送信</button></form>';
  detailEl.querySelector('h2').textContent=`${ticket.username} との購入DM`;detailEl.querySelector('.badge').textContent=ticket.deleted?'削除済み':(labels[ticket.status]||ticket.status);detailEl.querySelector('.site-id').textContent=ticket.account_public_id;detailEl.querySelector('.purchase-key').textContent=ticket.purchase_key;detailEl.querySelector('.vip-state').textContent=ticket.is_vip?(ticket.vip_expires_at?`VIP期限 ${fmt(ticket.vip_expires_at)}`:'VIP契約中'):'現在Free';detailEl.querySelector('.chat-state').textContent=ticket.deleted?`削除 ${fmt(ticket.deleted_at)}`:(ticket.chat_closed?'DM終了':'DM送信可能');detailEl.querySelector('.plan').textContent=`VIP ${ticket.requested_days}日`;detailEl.querySelector('.price').textContent=yen(ticket.price_yen);detailEl.querySelector('.payment').textContent=ticket.payment_link?'受信済み':'待機中';
  const statusRow=detailEl.querySelector('.status-row');for(const [value,label] of [['pending','申請受付'],['awaiting_payment','支払い待ち'],['payment_review','支払い確認中'],['completed','VIP付与完了'],['cancelled','キャンセル']]){const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=label;button.disabled=ticket.status===value;button.addEventListener('click',()=>setTicketStatus(value));statusRow.appendChild(button);}
  const deleteButton=detailEl.querySelector('.delete-ticket');deleteButton.hidden=Boolean(ticket.deleted);if(!ticket.deleted)deleteButton.addEventListener('click',closeTicket);detailEl.querySelector('.manual-status').hidden=Boolean(ticket.deleted);
  const nextText=detailEl.querySelector('.next-action strong');const nextButtons=detailEl.querySelector('.next-buttons');
  if(ticket.deleted){nextText.textContent='削除済みの会話ログです。閲覧のみできます。';}
  else if(ticket.status==='completed'){nextText.textContent='VIP付与済みです。必要な案内はDMで続けられます。';}
  else if(ticket.status==='cancelled'){nextText.textContent=ticket.chat_closed?'この申請とDMは終了しています。':'申請はキャンセル済みですが、DMは引き続き送信できます。';}
  else if(ticket.payment_link){nextText.textContent='PayPayリンクを確認し、申請プランのVIPを付与してください。';const grant=document.createElement('button');grant.type='button';grant.className='btn primary';grant.textContent=`VIP ${ticket.requested_days}日を付与`;grant.addEventListener('click',()=>grantVip(ticket.account_public_id,ticket.requested_days,ticket.purchase_key,grant));nextButtons.appendChild(grant);}
  else{nextText.textContent='利用者からPayPayの送金リンクが届くまでお待ちください。';}
  if(ticket.payment_link){const card=document.createElement('div');card.className='paypay-card';const code=document.createElement('code');code.textContent=ticket.payment_link;const open=document.createElement('a');open.className='btn primary';open.href=ticket.payment_link;open.target='_blank';open.rel='noopener noreferrer';open.textContent='PayPayリンクを開く';const copy=document.createElement('button');copy.type='button';copy.className='btn';copy.textContent='コピー';copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(ticket.payment_link);setStatus('PayPayリンクをコピーしました。');}catch{setStatus('コピーできませんでした。',true);}});card.append(code,open,copy);detailEl.querySelector('.paypay-slot').appendChild(card);}
  const quick=detailEl.querySelector('.quick-row');if(ticket.deleted){quick.hidden=true;}else{appendTemplateButton(quick,'支払い案内',String(config.paymentGuide||'お支払い方法をご案内します。'));appendTemplateButton(quick,'確認します','ご連絡ありがとうございます。内容を確認しますので、しばらくお待ちください。');appendTemplateButton(quick,'付与後案内','VIPを付与しました。画面を再読み込みしてVIPページをご利用ください。');appendTemplateButton(quick,'質問への返信','お問い合わせありがとうございます。');}
  const messages=detailEl.querySelector('.messages');const fragment=document.createDocumentFragment();for(const item of ticket.messages||[]){const row=document.createElement('article');row.className=`message ${item.sender_role}`;const sender=document.createElement('div');sender.className='sender';sender.textContent=`${item.sender_label}・${fmt(item.created_at)}`;const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=item.content;row.append(sender,bubble);fragment.appendChild(row);}messages.replaceChildren(fragment);messages.scrollTop=messages.scrollHeight;
  const composer=detailEl.querySelector('.composer');composer.style.display=(ticket.chat_closed||ticket.deleted)?'none':'grid';if(!ticket.deleted)composer.addEventListener('submit',sendMessage);
}

async function load(silent=false,forceDetail=false){
  if(state.loading)return;
  state.loading=true;
  try{
    const query=state.key?`?ticket=${encodeURIComponent(state.key)}`:'';
    const data=await api(`/api/admin/vip/state${query}`);
    state.data=data;
    document.getElementById('metric-accounts').textContent=data.summary.accounts;
    document.getElementById('metric-vip').textContent=data.summary.active_vip;
    document.getElementById('metric-pending').textContent=data.summary.pending;
    document.getElementById('metric-conversion').textContent=`${data.summary.conversion_30d}%`;
    const allTickets=data.tickets||[];
    document.getElementById('tab-unhandled').textContent=`未対応 ${data.summary.unhandled}`;
    document.getElementById('tab-working').textContent=`対応中 ${data.summary.working}`;
    document.getElementById('tab-done').textContent=`対応済 ${data.summary.done}`;
    document.getElementById('tab-deleted').textContent=`削除済み ${data.summary.deleted}`;
    document.getElementById('tab-vip').textContent=`VIP ${data.summary.active_vip}`;
    document.getElementById('tab-accounts').textContent=`アカウント ${data.summary.accounts}`;
    const selectedKey=String(data.selected?.purchase_key||'');
    if(!state.key&&selectedKey)state.key=selectedKey;
    if(!state.tabInitialized&&data.selected){activateTicketTab(statusGroup(data.selected));state.tabInitialized=true;}
    renderTickets(allTickets);renderSubscribers(data.subscribers||[]);renderAccounts(data.accounts||[]);
    const editor=detailEl.querySelector('.composer textarea');
    const editing=editor&&(document.activeElement===editor||editor.value.length>0);
    if(forceDetail||selectedKey!==state.detailKey||!editing)renderDetail(data.selected||null);
  }catch(error){if(!silent)setStatus(error.message,true);}
  finally{state.loading=false;}
}

async function sendMessage(event){event.preventDefault();const textarea=event.currentTarget.querySelector('textarea');const content=textarea.value.trim();if(!content||!state.key)return;const button=event.currentTarget.querySelector('button');button.disabled=true;try{await api('/api/admin/vip/messages',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key,content})});textarea.value='';textarea.blur();await load(true,true);}catch(error){setStatus(error.message,true);}finally{button.disabled=false;}}
async function setTicketStatus(value){if(!state.key)return;try{await api('/api/admin/vip/ticket-status',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key,status:value})});activateTicketTab(statusGroup(value));setStatus('申請状態を更新しました。DMはそのまま継続できます。');await load(false,true);}catch(error){setStatus(error.message,true);}}
async function closeTicket(){if(!state.key||!confirm('この購入DMを閉じますか？\n\n通常一覧から削除されます。会話ログは管理画面の「削除済み」に保存されます。VIP契約自体は削除されません。'))return;const button=detailEl.querySelector('.delete-ticket');if(button)button.disabled=true;try{await api('/api/admin/vip/close',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({purchase_key:state.key})});state.key='';state.detailKey='';state.tabInitialized=false;history.replaceState(null,'','/admin/vip');setStatus('購入チケットを削除済みに移動しました。');await load(false,true);}catch(error){setStatus(error.message,true);if(button)button.disabled=false;}}

async function grantVip(publicId,durationDays,purchaseKey='',button=null,preserveView=false){if(!confirm(`${publicId} にVIP ${durationDays}日を付与・延長しますか？\n\n付与後も購入DMは閉じません。`))return;if(button)button.disabled=true;try{const data=await api('/api/admin/vip/grant',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({public_id:publicId,duration_days:durationDays,purchase_key:purchaseKey})});if(!preserveView)activateTicketTab('done');setStatus(`${data.account.username} にVIPを付与しました。${purchaseKey?'DMは継続中です。':''}`);await load(false,true);}catch(error){setStatus(error.message,true);}finally{if(button)button.disabled=false;}}
document.getElementById('grant-form').addEventListener('submit',event=>{event.preventDefault();grantVip(document.getElementById('grant-id').value.trim().toUpperCase(),Number(document.getElementById('grant-days').value),document.getElementById('grant-key').value.trim().toUpperCase(),event.currentTarget.querySelector('button'));});
async function revokeVip(publicId,username){if(!confirm(`${username}（${publicId}）のVIPを剥奪しますか？`))return;try{await api('/api/admin/vip/revoke',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({public_id:publicId})});setStatus('VIPを剥奪しました。');await load(false,true);}catch(error){setStatus(error.message,true);}}
async function grantVipForAccount(item){const raw=prompt(`${item.username}（${item.public_id}）へ付与する日数を入力してください。\n30・60・90のいずれか`,'30');if(raw===null)return;const days=Number(raw);if(![30,60,90].includes(days)){setStatus('VIP期間は30・60・90日から選んでください。',true);return;}await grantVip(item.public_id,days,'',null,true);}
async function renameAccount(item){
  const username=prompt(`${item.public_id} の新しいユーザー名を入力してください。\n2〜24文字・空白不可`,item.username);
  if(username===null||username.trim()===item.username)return;
  try{await api('/api/admin/accounts/update',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({public_id:item.public_id,action:'rename',username:username.trim()})});setStatus('ユーザー名を変更しました。');await load(false,true);}catch(error){setStatus(error.message,true);}
}
async function manageAccount(publicId,username,action){
  const warnings={suspend:`${username}（${publicId}）を一時停止しますか？`,activate:`${username}（${publicId}）の一時停止を解除しますか？`,ban:`${username}（${publicId}）をBANしますか？`,unban:`${username}（${publicId}）のBANを解除しますか？`,gban:`${username}（${publicId}）をGBANしますか？\n\n同じIPまたはブラウザ指紋でアクセスした接続元も対象になり、Discord招待へ転送されます。`,ungban:`${username}（${publicId}）のGBANを解除しますか？`};
  if(!confirm(warnings[action]||'アカウント状態を更新しますか？'))return;
  try{await api('/api/admin/accounts/manage',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({public_id:publicId,action})});const messages={suspend:'一時停止しました。',activate:'一時停止を解除しました。',ban:'BANしました。',unban:'BANを解除しました。',gban:'GBANしました。対象アクセスはDiscord招待へ転送されます。',ungban:'GBANを解除しました。'};setStatus(messages[action]||'アカウント状態を更新しました。');await load(false,true);}catch(error){setStatus(error.message,true);}
}

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{state.tabInitialized=true;document.querySelectorAll('.tab').forEach(item=>item.classList.toggle('active',item===tab));document.querySelectorAll('.side-panels').forEach(panel=>panel.classList.toggle('active',panel.id===tab.dataset.view));if(tab.dataset.status){state.statusFilter=tab.dataset.status;if(state.data)renderTickets(state.data.tickets||[]);}}));
document.getElementById('account-filter').addEventListener('input',event=>{state.filter=event.currentTarget.value.trim().toLocaleLowerCase('ja');if(!state.data)return;renderTickets(state.data.tickets||[]);renderSubscribers(state.data.subscribers||[]);renderAccounts(state.data.accounts||[]);});
document.getElementById('revoke-by-id').addEventListener('click',()=>{const publicId=document.getElementById('grant-id').value.trim().toUpperCase();if(!/^[A-Z0-9]{16}$/.test(publicId)){setStatus('16桁のサイトIDを入力してください。',true);return;}const account=(state.data?.accounts||[]).find(item=>item.public_id===publicId);revokeVip(publicId,account?.username||'このアカウント');});
function base64UrlToUint8Array(value){const padding='='.repeat((4-value.length%4)%4);const raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from([...raw].map(ch=>ch.charCodeAt(0)));}
async function notificationState(){if(!('serviceWorker'in navigator)||!('PushManager'in window)||!('Notification'in window)||!config.vapidPublicKey){notifyButton.disabled=true;notifyButton.textContent='通知非対応';return;}const registration=await navigator.serviceWorker.register('/account-sw.js',{scope:'/'});const subscription=await registration.pushManager.getSubscription();if(subscription)await api('/api/account/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({admin:true,subscription:subscription.toJSON()})});notifyButton.classList.toggle('on',Boolean(subscription));notifyButton.textContent=subscription?'DM通知オン':'DM通知をオン';return {registration,subscription};}
notifyButton.addEventListener('click',async()=>{try{const current=await notificationState();if(!current)return;if(current.subscription){await api('/api/account/push/subscribe',{method:'DELETE',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({admin:true,endpoint:current.subscription.endpoint})});await current.subscription.unsubscribe();setStatus('管理者DM通知をオフにしました。');}else{const permission=await Notification.requestPermission();if(permission!=='granted')throw new Error('通知が許可されませんでした。');const subscription=await current.registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64UrlToUint8Array(config.vapidPublicKey)});await api('/api/account/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrfToken},body:JSON.stringify({admin:true,subscription:subscription.toJSON()})});setStatus('管理者DM通知をオンにしました。');}await notificationState();}catch(error){setStatus(error.message,true);}});

load();notificationState().catch(()=>{});state.timer=setInterval(()=>load(true),2500);
