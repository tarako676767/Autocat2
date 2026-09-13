'use strict';

self.addEventListener('push',event=>{
  let data={title:'AUTOCAT JP',body:'VIP購入DMに新着があります。',url:'/vip/purchase',tag:'autocat-vip-purchase'};
  try{if(event.data)data={...data,...event.data.json()};}catch{}
  event.waitUntil(self.registration.showNotification('AUTOCAT JP',{
    body:String(data.body||'VIP購入DMに新着があります。'),
    tag:String(data.tag||'autocat-vip-purchase'),
    data:{url:String(data.url||'/vip/purchase')},
  }));
});

self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=new URL(event.notification.data?.url||'/vip/purchase',self.location.origin).href;
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(windows=>{
    for(const client of windows){
      if('focus'in client){client.navigate(target);return client.focus();}
    }
    return clients.openWindow?clients.openWindow(target):undefined;
  }));
});
