(() => {
  const get=id=>document.getElementById('voice-'+id), panel=get('panel'), text=get('transcript');
  const record=get('record'), send=get('send'), process=get('process'), choices=get('choices');
  const synth=window.speechSynthesis, token=document.querySelector('meta[name="csrf-token"]').content;
  let recorder=null, stream=null, timer=null, ticker=null, generation=0, busy=false, modified=false;
  let previous=null, lastResult=null, lastReply='', pending=null, abort=null, turns=[], requestingMicrophone=false, selectedItem=null;
  try{const saved=JSON.parse(sessionStorage.getItem('studycenter_ice.voice.pending')||'null');if(saved&&typeof saved.text==='string'&&saved.text.length<=2000&&/^[0-9a-f-]{36}$/i.test(saved.request_id||''))pending=saved;}catch(_){}
  function savePending(){try{if(pending)sessionStorage.setItem('studycenter_ice.voice.pending',JSON.stringify(pending));else sessionStorage.removeItem('studycenter_ice.voice.pending');}catch(_){}}
  get('retry').onclick=()=>{if(pending&&!busy&&!transcribing&&!stream&&!requestingMicrophone)command(pending);};
  let transcribing=false, audioContext=null, meterFrame=null, draft='', editVersion=0;
  function saveDraft(){try{sessionStorage.setItem('studycenter_ice.voice.draft',text.value);}catch(_){}}
  try{text.value=sessionStorage.getItem('studycenter_ice.voice.draft')||'';}catch(_){}
  text.addEventListener('input',()=>{editVersion++;saveDraft();});
  function controls(){send.disabled=busy||transcribing||!!stream||requestingMicrophone;record.disabled=busy||transcribing||requestingMicrophone;get('pending').hidden=!pending||busy||transcribing||!!stream||requestingMicrophone;get('pending-text').textContent=pending?.text||'';get('new-chat').hidden=!turns.length&&!selectedItem;get('new-chat').disabled=busy||transcribing||!!stream||requestingMicrophone||(!turns.length&&!selectedItem);panel.setAttribute('aria-busy',String(busy||transcribing));}
  function showDraft(value){draft=value;get('draft-text').textContent=value;get('draft').hidden=!value;}
  get('use-draft').onclick=()=>{text.value=draft;editVersion++;saveDraft();showDraft('');text.focus();};
  get('discard-draft').onclick=()=>showDraft('');
  try {const saved=JSON.parse(sessionStorage.getItem('studycenter_ice.voice.chat')||'{}');turns=Array.isArray(saved.turns)?saved.turns.filter(t=>['user','assistant'].includes(t?.role)&&typeof t.text==='string').slice(-10):[];previous=saved.previous||null;lastResult=saved.lastResult||null;lastReply=typeof saved.lastReply==='string'?saved.lastReply:'';get('auto').checked=localStorage.getItem('studycenter_ice.voice.auto')==='true';} catch (_) {}
  const status=(id,message)=>{get(id).textContent=message;};
  function stopSpeaking(){synth?.cancel();status('tts-state','Sprachausgabe beendet.');}
  function populateVoices(){
    let selected=get('voice').value;try{selected=selected||localStorage.getItem('studycenter_ice.voice.voice')||'';}catch(_){}
    get('voice').replaceChildren(new Option('Systemstimme · Deutsch',''));
    const list=(synth?.getVoices()||[]).filter(v=>v.lang.toLowerCase().startsWith('de'));
    list.sort((a,b)=>(b.lang.toLowerCase()==='de-at')-(a.lang.toLowerCase()==='de-at'));
    list.forEach(v=>get('voice').add(new Option(`${v.name} · ${v.lang}`,v.voiceURI)));
    if([...get('voice').options].some(v=>v.value===selected))get('voice').value=selected;
    status('tts-state',list.length?'Deutsche Sprachausgabe bereit.':'Keine deutsche Browserstimme verfügbar. Schriftliche Antworten funktionieren weiterhin.');
  }
  function speak(message){
    if(!synth||requestingMicrophone||stream||recorder?.state==='recording'||!panel.open||!message)return;
    stopSpeaking();const utterance=new SpeechSynthesisUtterance(message.slice(0,1200));
    const list=synth.getVoices();utterance.voice=list.find(v=>v.voiceURI===get('voice').value)||list.find(v=>v.lang.toLowerCase()==='de-at')||list.find(v=>v.lang.toLowerCase()==='de-de')||list.find(v=>v.lang.toLowerCase().startsWith('de'))||null;
    utterance.lang=utterance.voice?.lang||'de-AT';
    utterance.onstart=()=>status('tts-state','Antwort wird vorgelesen …');utterance.onend=()=>status('tts-state','Antwort vorgelesen.');
    utterance.onerror=e=>status('tts-state',`Sprachausgabe nicht verfügbar (${e.error}). Bitte „Antwort vorlesen“ anklicken oder Text lesen.`);synth.speak(utterance);
  }
  panel.addEventListener('keydown',event=>{
    if(event.key!=='Tab')return;
    const tabbable=[...panel.querySelectorAll('a[href],button:not(:disabled),input:not([type="hidden"]):not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex]:not([tabindex^="-"])')]
      .filter(element=>{
        const closedDetails=element.closest('details:not([open])');
        return element.getClientRects().length&&!element.closest('[hidden]')&&getComputedStyle(element).visibility!=='hidden'
          &&(!closedDetails||(element.matches('summary')&&element.parentElement===closedDetails));
      });
    if(!tabbable.length)return;
    if(event.shiftKey&&document.activeElement===tabbable[0]){event.preventDefault();tabbable.at(-1).focus();}
    else if(!event.shiftKey&&document.activeElement===tabbable.at(-1)){event.preventDefault();tabbable[0].focus();}
  });
  function renderChat(){
    get('chat').replaceChildren();turns.slice(-10).forEach(turn=>{const p=document.createElement('p'),label=document.createElement('strong'),body=document.createElement('span');p.className='voice-turn '+turn.role;label.textContent=turn.role==='user'?'Du':'StudyCenter-ICE';body.textContent=turn.text;p.append(label,body);get('chat').append(p);});get('chat').scrollTop=get('chat').scrollHeight;get('speak').disabled=!lastReply||!synth;get('speak').closest('.voice-speaking').hidden=false;get('empty').hidden=turns.length>0;controls();
  }
  function addTurn(role,message){turns.push({role,text:message});turns=turns.slice(-10);try{sessionStorage.setItem('studycenter_ice.voice.chat',JSON.stringify({turns,previous,lastReply,lastResult}));}catch(_){}renderChat();}
  async function call(url,payload){
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),180000);
    try{const response=await fetch(url,{method:'POST',body:JSON.stringify(payload),headers:{'X-CSRF-Token':token,'Content-Type':'application/json'},signal:controller.signal});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Anfrage fehlgeschlagen.');return result;}
    catch(error){if(error.name==='AbortError')throw new Error('Die Antwort dauert zu lange. Der Ausführungsstand ist noch nicht bestätigt.');throw error;}
    finally{clearTimeout(timeout);}
  }
  function choose(choice,title){if(busy)return;addTurn('user','Auswahl: '+title);pending={request_id:crypto.randomUUID(),text:'Auswahl: '+title,previous_id:previous,remember_alias:get('remember-alias').checked,preview:true,...choice};command(pending);}
  function renderResult(result){choices.replaceChildren();
      const focus=get('focus');
      if(focus){focus.replaceChildren();focus.hidden=!result.focus_item;
        if(result.focus_item){const link=document.createElement('a');link.href=result.focus_item.url;link.textContent=result.focus_item.title;focus.append('Bezug für „diesen Eintrag“: ',link);}}
      if(result.preview){
        const box=document.createElement('section');box.className='voice-draft voice-preview';
        const heading=document.createElement('strong');heading.textContent='Vorschau · '+result.preview.title;box.append(heading);
        const display=value=>{
          if(value===null||value===undefined||value==='')return '—';
          if(typeof value==='boolean')return value?'Ja':'Nein';
          if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value.split('-').reverse().join('.');
          if(/^\d{2}:\d{2}(:\d{2})?$/.test(value))return value.slice(0,5);
          if(/^\d{4}-\d{2}-\d{2}T/.test(value))return new Intl.DateTimeFormat('de-AT',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Vienna'}).format(new Date(value));
          return {open:'Offen',doing:'In Arbeit',done:'Erledigt'}[value]||String(value);
        };
        const meta=document.createElement('small');meta.textContent=[result.preview.course,result.preview.date?display(result.preview.date):'Ohne Datum',result.preview.time?display(result.preview.time):'Ohne Uhrzeit'].filter(Boolean).join(' · ');if(!result.preview.tasks)box.append(meta);
        (result.preview.tasks||[]).forEach(task=>{const card=document.createElement('article'),title=document.createElement('strong'),details=document.createElement('small');card.className='voice-task-preview';title.textContent=task.title;details.textContent=[task.course,task.date?display(task.date):'Ohne Datum',task.time?display(task.time):'',task.pinned?'Angeheftet':''].filter(Boolean).join(' · ');card.append(title,details);if(task.description){const description=document.createElement('p');description.textContent=task.description;card.append(description);}box.append(card);});
        const list=document.createElement('dl');
        result.preview.changes.filter(change=>change.before!==change.after&&!(change.before==null&&(change.after==null||change.after===''||change.after===false))).forEach(change=>{const label=document.createElement('dt'),value=document.createElement('dd');label.textContent=change.label;value.textContent=change.before==null?display(change.after):display(change.before)+' → '+display(change.after);list.append(label,value);});if(list.children.length)box.append(list);
        const apply=document.createElement('button');apply.type='button';apply.textContent='Übernehmen';apply.onclick=()=>choose({apply_preview:true},'Änderung übernehmen');box.append(apply);
        const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Abbrechen';cancel.onclick=()=>choose({cancel:true},'Abbrechen');box.append(cancel);choices.append(box);
      }
      get('alias-choice').hidden=!(result.clarify&&result.courses?.length&&result.pending_action?.course);get('remember-alias').checked=false;
      (result.documents||[]).forEach(document=>{const card=document.createElement('article'),link=document.createElement('a'),meta=document.createElement('small');card.className='voice-task-preview';link.href=document.url;link.textContent=document.title;meta.textContent=[document.type,document.course,'Seite / Abschnitt '+document.page].filter(Boolean).join(' · ');card.append(link,meta);choices.append(card);});
      if(result.sources?.length){const group=document.createElement('div');group.className='voice-sources';const label=document.createElement('strong');label.textContent='Fundstellen aus deinen gespeicherten Unterlagen';group.append(label);result.sources.forEach(source=>{const detail=document.createElement('details'),summary=document.createElement('summary'),link=document.createElement('a'),meta=document.createElement('small');link.href=source.url;link.textContent=source.title+(source.page?' · Seite '+source.page:'');meta.textContent=[source.course,source.published_at?'Veröffentlicht: '+source.published_at.slice(0,10):'Veröffentlichungsdatum unbekannt'].filter(Boolean).join(' · ');summary.append(link);detail.append(summary,meta);[...new Set(source.quotes||[])].forEach(quote=>{const block=document.createElement('blockquote');block.textContent=quote;detail.append(block);});group.append(detail);});choices.append(group);}
      (result.items||[]).forEach(item=>{const link=document.createElement('a');link.href=item.url;link.textContent=item.title;choices.append(link);if(result.clarify&&result.pending_action&&!result.dialog){const button=document.createElement('button');button.type='button';button.className='secondary small';button.textContent='Diesen Eintrag wählen';button.onclick=()=>choose({choice_id:item.id},item.title);choices.append(button);}});
      (result.dialog?[]:result.courses||[]).forEach(course=>{const button=document.createElement('button');button.type='button';button.className='secondary small';button.textContent=course.title;button.onclick=()=>choose({choice_course_id:course.id},course.title);choices.append(button);});
      if((result.created||result.undoable)&&!result.undone){const button=document.createElement('button');button.type='button';button.className='secondary small';button.textContent=result.direct?'Rückgängig':result.created?'Erstellung rückgängig':'Änderung rückgängig';button.onclick=async()=>{
        if(busy||transcribing||stream||requestingMicrophone)return;
        busy=true;controls();choices.querySelectorAll('button').forEach(b=>b.disabled=true);status('process','Änderung wird rückgängig gemacht …');
        try{const undone=await call('/api/voice/undo',{request_id:result.request_id,confirm:true});modified=true;lastResult=undone;lastReply=undone.message;renderResult(undone);addTurn('assistant',undone.message);status('process','Änderung rückgängig gemacht.');if(get('auto').checked)speak(undone.message);if(!panel.open)location.reload();}
        catch(error){status('process',error.message);}
        finally{busy=false;choices.querySelectorAll('button').forEach(b=>b.disabled=false);controls();}
      };choices.append(button);}
      if(result.has_more){const more=document.createElement('button');more.type='button';more.className='secondary small';more.textContent='Weitere Treffer';more.onclick=()=>choose({more:true},'Weitere Treffer');choices.append(more);}
      if(result.navigate){const link=document.createElement('a');link.href=result.navigate;link.textContent='Ansicht öffnen →';choices.append(link);}

      (result.dialog?.options||[]).forEach(option=>{const button=document.createElement('button');button.type='button';button.className='secondary small';button.textContent=option.label;button.onclick=()=>{if(option.id==='time:input'){text.focus();return;}choose({option_id:option.id},option.label);};choices.append(button);});
      const waiting=!!result.dialog?.waiting;get('question').hidden=!waiting||!!result.preview;get('question-text').textContent=waiting?result.message:'';
      const action=result.dialog?.action||{};get('context').textContent=waiting?[action.title,action.course||((result.courses||[]).find(c=>c.id===action.course_id)?.title),action.date,action.time].filter(Boolean).join(' · '):'';
      send.textContent=waiting?'Antwort an Luna senden':'Mit Luna senden';
  }
  async function command(payload){
    // A restored pre-preview request keeps its old semantics and request identity.
    payload={preview:false,...payload};
    savePending();busy=true;controls();send.disabled=true;record.disabled=true;choices.querySelectorAll('button').forEach(b=>b.disabled=true);status('process','StudyCenter-ICE prüft deine Eingabe …');
    try{
      const result=await call('/api/voice/command',payload);pending=null;savePending();if(!result.failed&&text.value.trim()===payload.text){text.value='';editVersion++;saveDraft();}if(!result.failed){previous=result.request_id;lastResult=result;}lastReply=result.spoken_message||result.message;modified=modified||result.changed;addTurn('assistant',result.message);
      if(!result.failed)renderResult(result);
      status('process',result.failed?'Nicht ausgeführt.':result.changed?(result.direct?'Direkt ausgeführt':'Gespeichert')+' · beim Schließen wird die Ansicht aktualisiert.':result.insufficient?'Keine zuverlässige Antwort · vorhandene Fundstellen bleiben prüfbar.':'Bereit.');if(get('auto').checked)speak(lastReply);if(result.changed&&!panel.open)location.reload();
      if(!result.failed&&result.navigate&&/^\/(?:$|overview(?:\?|$)|board(?:\?|$)|pins(?:\?|$)|calendar(?:\?|$)|archive(?:\?|$)|sources(?:\?|$)|documents(?:\?|$)|search(?:\?|$)|settings(?:\?|$)|items\/\d+$)/.test(result.navigate))location.assign(result.navigate);
    }catch(error){status('process',error.message+' Dieselbe Eingabe kann erneut gesendet werden.');}
    finally{busy=false;if(!pending)choices.querySelectorAll('button').forEach(b=>b.disabled=false);controls();}
  }
  get('command-form').addEventListener('submit',event=>{event.preventDefault();if(busy||transcribing||requestingMicrophone||stream||recorder?.state==='recording')return;const value=text.value.trim();if(!value)return;if(!pending||pending.text!==value){pending={request_id:crypto.randomUUID(),text:value,previous_id:previous,preview:true,...(!previous&&selectedItem?{context_item_id:selectedItem.id}:{})};addTurn('user',value);}stopSpeaking();command(pending);});
  function stopMeter(){
    cancelAnimationFrame(meterFrame);meterFrame=null;
    const context=audioContext;audioContext=null;context?.close().catch(()=>{});
    get('meter').value=0;get('meter').hidden=true;
  }
  function startMeter(acquired,attempt){
    const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)return;
    try{
      audioContext=new Audio();const source=audioContext.createMediaStreamSource(acquired),analyser=audioContext.createAnalyser();analyser.fftSize=512;source.connect(analyser);
      const values=new Float32Array(analyser.fftSize);get('meter').hidden=false;
      const tick=()=>{if(attempt!==generation||!stream)return;analyser.getFloatTimeDomainData(values);const rms=Math.sqrt(values.reduce((sum,n)=>sum+n*n,0)/values.length);get('meter').value=Math.min(1,rms*5);meterFrame=requestAnimationFrame(tick);};tick();
    }catch(_){stopMeter();}
  }
  function release(){
    clearTimeout(timer);clearInterval(ticker);stopMeter();stream?.getTracks().forEach(t=>t.stop());stream=null;requestingMicrophone=false;
    record.textContent='Aufnehmen';record.closest('.voice-recording').dataset.recording='false';status('record-state','Mikrofon aus');controls();
  }
  record.onclick=async()=>{
    if(recorder?.state==='recording'){record.disabled=true;recorder.stop();return;}
    if(busy||transcribing||requestingMicrophone)return;stopSpeaking();
    if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){status('record-state','Audioaufnahme nicht verfügbar. Bitte Text eingeben.');return;}
    const attempt=++generation, originalText=text.value, originalEdit=editVersion;requestingMicrophone=true;controls();status('record-state','Mikrofonfreigabe wird angefragt …');
    try{
      const acquired=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
      if(!panel.open||attempt!==generation){acquired.getTracks().forEach(t=>t.stop());return;}stream=acquired;requestingMicrophone=false;
      const mime=['audio/ogg;codecs=opus','audio/webm;codecs=opus','audio/webm','audio/mp4'].find(v=>MediaRecorder.isTypeSupported(v));if(!mime)throw new Error('Kein unterstütztes Audioformat.');
      recorder=new MediaRecorder(stream,{mimeType:mime});const currentRecorder=recorder;let chunks=[],size=0,failed=false;
      recorder.ondataavailable=e=>{if(attempt===generation&&e.data.size){chunks.push(e.data);size+=e.data.size;if(size>16*1024*1024&&currentRecorder.state==='recording')currentRecorder.stop();}};
      recorder.onerror=()=>{failed=true;acquired.getTracks().forEach(t=>t.stop());if(attempt===generation){release();status('record-state','Aufnahme fehlgeschlagen. Bitte Text eingeben.');}};
      recorder.onstop=async()=>{
        const audio=new Blob(chunks,{type:mime});chunks=[];
        if(!panel.open||attempt!==generation||failed){acquired.getTracks().forEach(t=>t.stop());return;}
        release();if(size>16*1024*1024||!size){status('process',size?'Aufnahme zu groß. Bitte kürzer aufnehmen.':'Die Aufnahme enthält kein Audio. Bitte erneut aufnehmen.');return;}
        transcribing=true;controls();status('process','Lokale Spracherkennung läuft …');
        const controller=new AbortController(), statusController=new AbortController();abort=controller;const started=Date.now();
        let phase='', checking=false;
        const showProgress=()=>{if(attempt===generation&&panel.open)status('process',`Lokale Spracherkennung · ${Math.floor((Date.now()-started)/1000)} s${phase?' · '+phase:''} …`);};
        const checkPhase=async()=>{
          if(checking||attempt!==generation||!panel.open)return;checking=true;
          try{const response=await fetch('/api/voice/status',{signal:statusController.signal});if(!response.ok)return;const state=await response.json();if(!statusController.signal.aborted&&attempt===generation&&panel.open){phase=state.stt_busy&&typeof state.stt_phase==='string'?state.stt_phase:'';showProgress();}}
          catch(_){}finally{checking=false;}
        };
        const progress=setInterval(showProgress,1000), phasePoll=setInterval(checkPhase,2000);checkPhase();
        const timeout=setTimeout(()=>controller.abort(),180000);
        try{
          const response=await fetch('/api/voice/transcribe',{method:'POST',body:audio,headers:{'X-CSRF-Token':token,'Content-Type':mime},signal:controller.signal});const result=await response.json();
          if(attempt!==generation||!panel.open)return;if(!response.ok)throw new Error(result.detail||'Spracherkennung fehlgeschlagen.');
          if(editVersion!==originalEdit||text.value!==originalText||originalText.trim()){showDraft(result.text);}else{text.value=result.text;saveDraft();showDraft('');}
          status('course-hints',(result.course_suggestions?.length?'Kurskandidaten: '+result.course_suggestions.map(c=>c.title).join(' · ')+'. Transkript unverändert. ':'')+(result.warning||''));
          status('process',`Transkript bereit · ${result.engine}/${result.model} · ${result.device} · ${result.latency}s. Prüfen und mit Luna senden.`);
        }catch(error){if(attempt===generation&&panel.open)status('process',error.name==='AbortError'?'Spracherkennung dauert zu lange. Texteingabe bleibt verfügbar.':error.message+' Texteingabe bleibt verfügbar.');}
        finally{clearInterval(progress);clearInterval(phasePoll);statusController.abort();clearTimeout(timeout);if(attempt===generation){transcribing=false;abort=null;controls();}}
      };
      recorder.start(1000);startMeter(acquired,attempt);controls();record.textContent='Aufnahme beenden';record.closest('.voice-recording').dataset.recording='true';
      const started=Date.now();status('record-state','Aufnahme läuft · 0 / 60 Sekunden');ticker=setInterval(()=>status('record-state',`Aufnahme läuft · ${Math.floor((Date.now()-started)/1000)} / 60 Sekunden`),1000);
      timer=setTimeout(()=>{if(currentRecorder.state==='recording')currentRecorder.stop();},60000);
    }catch(error){if(attempt===generation){release();const reason=error.name==='NotAllowedError'?'Zugriff nicht erlaubt':error.name==='NotFoundError'?'kein Mikrofon gefunden':error.name==='NotReadableError'?'Mikrofon wird bereits verwendet':error.message;status('record-state',`Mikrofon nicht verfügbar: ${reason}. Bitte Text eingeben.`);}}
    finally{if(attempt===generation)controls();}
  };
  get('open').onclick=async()=>{panel.showModal();renderChat();if(lastResult)renderResult(lastResult);text.focus({preventScroll:true});panel.scrollTop=0;populateVoices();try{const response=await fetch('/api/voice/status'),state=await response.json();status('connection',`${state.engine} ${state.model} · ${state.device}${state.local_model?' · lokal bereit':' · Modelldateien noch nicht bereit'} · ${state.connected?state.llm_model+' verbunden':'ChatGPT-Verbindung/Live-Probe erforderlich'}${state.stt_warning?' · '+state.stt_warning:''}`);}catch(_){status('process','Verbindungsstatus nicht verfügbar.');}};
  get('close').onclick=()=>panel.close();panel.onclose=()=>{generation++;abort?.abort();abort=null;transcribing=false;if(recorder?.state==='recording')recorder.stop();release();stopSpeaking();if(modified)location.reload();};
  get('new-chat').onclick=async()=>{if(busy||transcribing||stream||requestingMicrophone)return;if(lastResult?.dialog?.waiting){busy=true;controls();try{await call('/api/voice/command',{request_id:crypto.randomUUID(),text:'Abbrechen',previous_id:previous,cancel:true});}catch(error){status('process',error.message);busy=false;controls();return;}busy=false;}stopSpeaking();selectedItem=null;turns=[];previous=null;lastResult=null;lastReply='';if(get('focus')){get('focus').hidden=true;get('focus').replaceChildren();}get('question').hidden=true;send.textContent='Mit Luna senden';pending=null;savePending();choices.replaceChildren();get('alias-choice').hidden=true;showDraft('');status('course-hints','');try{sessionStorage.removeItem('studycenter_ice.voice.chat');}catch(_){}renderChat();status('process','Neue Unterhaltung. Deine gespeicherten Einträge bleiben erhalten.');text.focus();};
  document.querySelectorAll('[data-voice-item]').forEach(button=>button.addEventListener('click',()=>{
    if(busy||transcribing||stream||requestingMicrophone){status('process','Bitte die laufende Eingabe zuerst abschließen.');return;}
    const id=Number(button.dataset.voiceItem);if(!Number.isSafeInteger(id)||id<=0)return;
    // Switching the card starts a fresh conversation; no old proposal is applied.
    stopSpeaking();turns=[];previous=null;lastResult=null;lastReply='';pending=null;savePending();selectedItem={id,title:button.dataset.voiceTitle||'Eintrag'};
    get('question').hidden=true;send.textContent='Mit Luna senden';choices.replaceChildren();get('alias-choice').hidden=true;showDraft('');status('course-hints','');
    text.value='';editVersion++;saveDraft();try{sessionStorage.removeItem('studycenter_ice.voice.chat');}catch(_){}
    get('open').click();
    const focus=get('focus');if(focus){const link=document.createElement('a');link.href='/items/'+id;link.textContent=selectedItem.title;focus.replaceChildren('Bezug für „diesen Eintrag“: ',link);focus.hidden=false;}
    renderChat();status('process','Neue Unterhaltung zu diesem Eintrag.');
  }));
  get('other-answer').onclick=()=>text.focus();
  document.querySelectorAll('[data-voice-example]').forEach(button=>button.addEventListener('click',()=>{
    if(busy||transcribing||stream||requestingMicrophone)return;
    text.value=button.dataset.voiceExample;editVersion++;saveDraft();text.focus({preventScroll:true});
  }));
  get('cancel').onclick=()=>{if(!busy&&lastResult?.dialog?.waiting){pending={request_id:crypto.randomUUID(),text:'Abbrechen',previous_id:previous,cancel:true};command(pending);}};
  get('speak').onclick=()=>speak(lastReply);get('stop').onclick=stopSpeaking;
  get('auto').onchange=()=>{try{localStorage.setItem('studycenter_ice.voice.auto',get('auto').checked);}catch(_){}if(!get('auto').checked)stopSpeaking();};get('voice').onchange=()=>{try{localStorage.setItem('studycenter_ice.voice.voice',get('voice').value);}catch(_){}};
  synth?.addEventListener('voiceschanged',populateVoices);populateVoices();
  function suspendAudio(){generation++;abort?.abort();abort=null;transcribing=false;if(recorder?.state==='recording')recorder.stop();release();synth?.cancel();}
  addEventListener('pagehide',suspendAudio);
  addEventListener('studycenter-hide',suspendAudio);
  if(location.hash==='#voice')get('open').click();
})();
