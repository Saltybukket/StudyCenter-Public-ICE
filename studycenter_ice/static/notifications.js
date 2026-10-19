(()=>{
  const open=document.querySelector('#notifications-open'),panel=document.querySelector('#notifications-panel');
  if(!open||!panel)return;
  const list=document.querySelector('#notifications-list'),badge=document.querySelector('#notifications-badge'),more=document.querySelector('#notifications-more'),previous=document.querySelector('#notifications-previous'),read=document.querySelector('#notifications-read');
  let filter='unread',page=1,entries=[],busy=false;
  const date=value=>new Date(value).toLocaleString('de-AT',{timeZone:'Europe/Vienna',dateStyle:'short',timeStyle:'short'});
  const text=(tag,value,style)=>{const node=document.createElement(tag);node.textContent=value;if(style)node.className=style;return node;};
  function render(){
    list.replaceChildren();
    if(!entries.length)list.append(text('p',filter==='unread'?'Keine ungelesenen Meldungen. Frühere Meldungen findest du im Verlauf.':'Noch keine ausgegebenen Meldungen in den letzten 90 Tagen.','muted'));
    for(const entry of entries){
      const row=document.createElement('article');row.className='notification-entry'+(entry.unread?' unread':'');
      const link=text('a',entry.title);link.href='/items/'+entry.item_id;row.append(link);
      row.append(text('small',[entry.course_title,date(entry.received_at)].filter(Boolean).join(' · ')));
      row.append(text('p',entry.labels.join(' · ')));
      row.append(text('small',entry.errors?`${entry.errors} Windows-Übergabe(n) fehlgeschlagen · In-App-Verlauf verfügbar`:`An Windows übergeben${entry.deliveries>1?' · '+entry.deliveries+' Meldungen':''}`));
      if(entry.unread){const button=text('button','Gelesen','secondary small');button.type='button';button.setAttribute('aria-label',entry.title+' als gelesen markieren');button.addEventListener('click',()=>mark(entry.ids));row.append(button);}
      list.append(row);
    }
    read.disabled=!entries.some(entry=>entry.unread)||busy;
  }
  function updateBadge(count){badge.hidden=!count;badge.textContent=count>99?'99+':String(count);open.setAttribute('aria-label',`Benachrichtigungen öffnen${count?' · '+count+' ungelesene Termine':''}`);}
  async function load(append=false){
    if(busy)return;busy=true;read.disabled=true;more.disabled=true;
    try{const data=await api(`/api/notifications?unread=${filter==='unread'}&page=${page}`);updateBadge(data.unread);entries=data.entries;more.hidden=!data.has_more;previous.hidden=page===1;render();}
    catch(error){entries=[];more.hidden=true;previous.hidden=true;list.replaceChildren(text('p',error.message,'warning'));}
    finally{busy=false;more.disabled=false;read.disabled=!entries.some(entry=>entry.unread);}
  }
  async function mark(ids){
    if(busy||!ids.length)return;
    busy=true;read.disabled=true;
    const errorNotice=document.querySelector('#notifications-error');errorNotice.hidden=true;
    const buttons=[...list.querySelectorAll('button')];buttons.forEach(button=>{button.disabled=true;});
    let saved=false;
    try{const result=await api('/api/notifications/read',{method:'POST',body:JSON.stringify({ids}),headers:{'Content-Type':'application/json'}});if(result.saved!==true)throw new Error('Lesebestätigung fehlt.');saved=true;}
    catch(error){errorNotice.textContent=error.message+' Speichern nicht bestätigt. Bitte erneut versuchen.';errorNotice.hidden=false;}
    finally{busy=false;buttons.forEach(button=>{button.disabled=false;});read.disabled=!entries.some(entry=>entry.unread);}
    if(saved){page=1;await load();}
  }
  function close(){panel.hidden=true;open.setAttribute('aria-expanded','false');}
  open.addEventListener('click',()=>{panel.hidden=!panel.hidden;open.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden){page=1;load();}});
  document.querySelector('#notifications-close').addEventListener('click',()=>{close();open.focus();});
  panel.querySelectorAll('[data-notification-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.notificationFilter;page=1;panel.querySelectorAll('[data-notification-filter]').forEach(option=>option.setAttribute('aria-pressed',String(option===button)));load();}));
  more.addEventListener('click',()=>{page++;load();});
  previous.addEventListener('click',()=>{page=Math.max(1,page-1);load();});
  read.addEventListener('click',()=>mark(entries.filter(entry=>entry.unread).flatMap(entry=>entry.ids).slice(0,200)));
  document.addEventListener('click',event=>{if(!event.target.closest('.notification-menu'))close();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){close();open.focus();}});
  async function refresh(){if(document.hidden||busy)return;if(!panel.hidden){await load();}else try{updateBadge((await api('/api/notifications?unread=true')).unread);}catch(_){}}
  setInterval(refresh,60000);document.addEventListener('visibilitychange',refresh);refresh();
})();
