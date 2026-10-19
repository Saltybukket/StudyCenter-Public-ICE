// Display-only shorthand. Original titles, source fields and export data stay intact.
const calendarAbbreviations = [
  ['Elektronische Schaltungstechnik 2', 'ES2'],
  ['Computer Organization and Networks', 'CON'],
  ['Wahrscheinlichkeitstheorie für Informatikstudien', 'WFI'],
  ['Entwurf von Echtzeitsystemen', 'EES'],
  ['Messtechnik', 'MT'],
  ['Physik für Electrical and Electronics Engineering', 'Physik'],
  ['Lehrveranstaltung', 'LV'], ['Vorlesung', 'VO'], ['Übung', 'UE']
];
function calendarShortLabel(value, limit = 62) {
  let text = String(value || '');
  for (const [full, short] of calendarAbbreviations) {
    const escaped = full.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    text = text.replace(new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, 'giu'), short);
  }
  text = text.replace(/\((VO|UE)\)\s*·\s*\1\b/g, '· $1');
  if (text.length <= limit) return text;
  const prefix = text.slice(0, limit - 1), boundary = prefix.lastIndexOf(' ');
  return (boundary > limit / 2 ? prefix.slice(0, boundary) : prefix) + '…';
}
// A work or registration window is not a continuous personal appointment.
function calendarIsWindow(event) {
  const p = event.extendedProps;
  return p.dateMeaning === 'window' || p.itemKind === 'registration' && Boolean(p.sourceEndDate)
    || p.sourceEndDate > p.sourceDate && (!p.personal || ['task','notice','registration'].includes(p.itemKind));
}
function calendarWeekEvents(source) {
  return source.flatMap(event => {
    if (!calendarIsWindow(event)) return [event];
    const p = event.extendedProps;
    if (!p.closingDate && p.itemKind !== 'registration') return [];
    const date = p.closingDate || p.sourceDate, clock = p.closingDate ? p.closingTime : p.sourceTime;
    const point = {...event, start:date + (clock ? 'T' + clock : ''), allDay:!clock,
      extendedProps:{...p, weekPoint:true}};
    delete point.end;
    return [point];
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const element = document.querySelector('#calendar'), error = document.querySelector('#calendar-error');
  if (!window.FullCalendar) { error.hidden = false; error.textContent = 'Lokale Kalenderdateien fehlen.'; return; }
  const course = document.querySelector('#calendar-course'), history = document.querySelector('#calendar-history');
  const kind = document.querySelector('#calendar-kind'), gantt = document.querySelector('#gantt');
  const dateInput = document.querySelector('#calendar-date'), title = document.querySelector('#calendar-title');
  const options = document.querySelector('#gantt-options'), points = document.querySelector('#gantt-points');
  const days = document.querySelector('#gantt-days'), explanation = document.querySelector('#calendar-explanation');
  const afterHours = document.querySelector('#calendar-after-hours');
  const afterHoursList = document.querySelector('#calendar-after-hours-list');
  const afterHoursCount = document.querySelector('#calendar-after-hours-count');
  const windowList = document.querySelector('#calendar-window-list'), windowItems = document.querySelector('#calendar-window-items');
  const windowCount = document.querySelector('#calendar-window-count');
  const dialog = document.querySelector('#calendar-event-dialog');
  let dialogOpener = null;
  const buttons = [...document.querySelectorAll('[data-calendar-view]')];
  const DAY = 86400000, today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Europe/Vienna'}).format(new Date());
  const dayNumber = iso => Date.parse(iso + 'T00:00:00Z') / DAY;
  const addDays = (iso, n) => new Date((dayNumber(iso) + n) * DAY).toISOString().slice(0, 10);
  const label = (iso, long = false) => new Intl.DateTimeFormat('de-AT', {timeZone:'UTC', day:'2-digit', month:'2-digit', ...(long ? {year:'numeric'} : {})}).format(new Date(iso + 'T12:00:00Z'));
  const timePart = time => time ? (Number(time.slice(0,2))*60 + Number(time.slice(3,5))) / 1440 : 0;
  let view = 'timeGridWeek', anchor = today, events = [], requestNumber = 0, visibleWeek = null, viewChosen = false;
  const query = new URLSearchParams(location.search), requestedDate = query.get('date');
  if (/^\d{4}-\d{2}-\d{2}$/.test(requestedDate || '') && Number.isFinite(dayNumber(requestedDate))) anchor = requestedDate;
  for (const [control, parameter] of [[course,'course'],[kind,'kind']]) {
    if ([...control.options].some(option => option.value === query.get(parameter))) control.value = query.get(parameter);
  }
  history.checked = query.get('include_inactive') === 'true';
  const exportCourse = document.querySelector('#export-course');
  const copyExportCourse = () => {
    exportCourse.value = [...exportCourse.options].some(o => o.value === course.value) ? course.value : '';
    document.querySelector('#calendar-export-link').href = '/calendar/export.ics?course=' + encodeURIComponent(exportCourse.value);
  };
  copyExportCourse();
  course.addEventListener('change', copyExportCourse);
  function syncFilterUrl() {
    const url = new URL(window.location.href);
    for (const [parameter, value] of [['course', course.value], ['kind', kind.value]]) {
      if (value) url.searchParams.set(parameter, value);
      else url.searchParams.delete(parameter);
    }
    if (history.checked) url.searchParams.set('include_inactive', 'true');
    else url.searchParams.delete('include_inactive');
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
  }
  try { const saved = localStorage.getItem('studycenter_ice.calendar.view'); if (buttons.some(b => b.dataset.calendarView === saved)) view = saved; } catch (_) {}
  if(buttons.some(button=>button.dataset.calendarView===query.get('view')))view=query.get('view');
  const rangeText = event => {
    const p = event.extendedProps;
    const start = label(p.sourceDate, true) + (p.sourceTime ? ' ' + p.sourceTime.slice(0,5) : ' · ohne Uhrzeit');
    if (p.sourceEndDate) return start + ' – ' + label(p.sourceEndDate, true) + (p.sourceEndTime ? ' ' + p.sourceEndTime.slice(0,5) : ' · ohne Endzeit');
    return start + (p.dateMeaning === 'deadline' ? ' · nur Frist, Beginn unbekannt' : ' · Ende unbekannt');
  };
  function showEvent(event, opener) {
    const p = event.extendedProps;
    const isClosing = Boolean(p.weekPoint && p.closingDate);
    const date = isClosing ? p.closingDate : p.sourceDate, clock = isClosing ? p.closingTime : p.sourceTime;
    let when = label(date, true) + (clock ? ' · ' + clock.slice(0,5) : ' · ohne Uhrzeit');
    if (!isClosing && p.sourceEndDate) {
      when += ' – ' + (p.sourceEndDate !== date ? label(p.sourceEndDate,true) + ' · ' : '')
        + (p.sourceEndTime ? p.sourceEndTime.slice(0,5) : 'ohne Endzeit');
    } else if (!isClosing) when += p.dateMeaning === 'deadline' ? ' · Frist' : ' · Ende offen';
    document.querySelector('#calendar-event-title').textContent = event.title;
    document.querySelector('#calendar-event-course').textContent = p.course;
    document.querySelector('#calendar-event-when').textContent = (isClosing ? (p.itemKind === 'registration' ? 'Anmeldeschluss · ' : 'Frist · ') : '') + when;
    document.querySelector('#calendar-event-kind').textContent = p.kind || '';
    document.querySelector('#calendar-event-status').textContent = {open:'Offen',doing:'In Arbeit',done:'Erledigt'}[p.status] || p.status;
    document.querySelector('#calendar-event-obligation').textContent = p.obligation || 'Verpflichtung unklar';
    document.querySelector('#calendar-event-personal').textContent = p.bookingProven ? 'Buchung durch Belege bestätigt.' : p.personallyPlanned ? 'Von dir geplant; kein offizieller Buchungsbeleg.' : p.personal ? 'Termin aus deinem persönlichen Kalender; bestätigt allein keinen konkreten Labslot.' : 'Persönliche Zuordnung offen.';
    const description = document.querySelector('#calendar-event-description');
    description.textContent = p.description || ''; description.hidden = !p.description;
    const warning = document.querySelector('#calendar-event-warning');
    warning.textContent = [p.conflict ? 'Widersprüchliche Quellenangaben: Belege prüfen.' : '', p.uncertainty || ''].filter(Boolean).join(' ');
    warning.hidden = !warning.textContent;
    document.querySelector('#calendar-event-open').href = event.url;
    document.querySelector('#calendar-event-evidence').href = event.url + '#evidence';
    document.querySelector('#calendar-event-work').href = event.url + '#my-work';
    document.querySelector('#calendar-event-source-range').textContent = rangeText(event);
    dialog.querySelector('details').open = false;
    dialogOpener = opener;
    dialog.showModal();
  }
  function bindEventLink(link, event) {
    link.setAttribute('aria-haspopup','dialog');
    link.addEventListener('click',click => {
      if (click.ctrlKey || click.metaKey || click.shiftKey || click.altKey) return;
      click.preventDefault(); showEvent(event,link);
    });
  }
  document.querySelector('#calendar-event-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>dialogOpener?.focus());
  dialog.addEventListener('click',event=>{if(event.target === dialog && (event.clientX < dialog.getBoundingClientRect().left || event.clientX > dialog.getBoundingClientRect().right || event.clientY < dialog.getBoundingClientRect().top || event.clientY > dialog.getBoundingClientRect().bottom)) dialog.close();});
  const abbreviationList = document.querySelector('#calendar-abbreviations');
  for (const [full,short] of calendarAbbreviations) {
    const item = document.createElement('span'), abbreviation = document.createElement('abbr');
    abbreviation.title = full; abbreviation.textContent = short;
    item.append(abbreviation, document.createTextNode(' · ' + full)); abbreviationList.append(item);
  }
  async function loadEvents() {
    const request = ++requestNumber;
    const response = await fetch(`/api/calendar?course=${encodeURIComponent(course.value)}&kind=${encodeURIComponent(kind.value)}&include_inactive=${history.checked}`);
    if (!response.ok) throw new Error('Kalender konnte nicht geladen werden. Bitte erneut versuchen.');
    const data = await response.json();
    if (request === requestNumber) {
      events = data; error.hidden = true;
      if (view === 'gantt') renderGantt();
      else { renderAfterHours(calendarWeekEvents(events), visibleWeek); renderWindows(); }
    }
    return data;
  }
  function showError(exception) { error.textContent = exception.message; error.hidden = false; }
  function wallDateTime(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(value || '');
    if (!match) return null;
    return Date.UTC(Number(match[1]),Number(match[2])-1,Number(match[3]),Number(match[4]),Number(match[5]),Number(match[6]||0));
  }
  function outsideDisplayedHours(event, weekStart, weekEnd) {
    if (event.allDay || !event.start || !event.url) return false;
    const start = wallDateTime(event.start), end = event.end ? wallDateTime(event.end) : null;
    const from = dayNumber(weekStart) * DAY, until = dayNumber(weekEnd) * DAY;
    if (start === null || end !== null && end <= start) return false;
    if (end === null ? start < from || start >= until : end <= from || start >= until) return false;
    for (let day=from; day<until; day+=DAY) {
      const opens=day+8*60*60*1000, closes=day+22*60*60*1000;
      if (end === null ? start >= opens && start < closes : start < closes && end > opens) return false;
    }
    return true;
  }
  function renderAfterHours(source, range) {
    afterHoursList.replaceChildren();
    if (view !== 'timeGridWeek' || !range) { afterHours.hidden=true; afterHoursCount.textContent=''; return; }
    const outside=source.filter(event=>outsideDisplayedHours(event,range.start,range.end))
      .sort((a,b)=>wallDateTime(a.start)-wallDateTime(b.start)||a.title.localeCompare(b.title,'de'));
    if (!outside.length) { afterHours.hidden=true; afterHoursCount.textContent=''; return; }
    afterHoursCount.textContent=`${outside.length} ${outside.length===1?'Termin':'Termine'}`;
    for (const event of outside) {
      const row=document.createElement('li'), time=document.createElement('time'), link=document.createElement('a');
      const [dateText,timeText]=event.start.split('T');
      time.dateTime=event.start; time.textContent=`${label(dateText,true)} · ${timeText.slice(0,5)}`;
      link.href=event.url; link.textContent=event.title;
      bindEventLink(link,event);
      row.append(time,link);
      const meta=[event.extendedProps?.course,event.extendedProps?.kind].filter(Boolean).join(' · ');
      if(meta){const detail=document.createElement('small');detail.textContent=meta;row.append(detail);}
      afterHoursList.append(row);
    }
    afterHours.hidden=false;
  }
  function renderWindows() {
    windowItems.replaceChildren();
    if (view !== 'timeGridWeek' || !visibleWeek) { windowList.hidden = true; return; }
    const windows = events.filter(event => calendarIsWindow(event)
      && event.extendedProps.sourceDate < visibleWeek.end
      && (event.extendedProps.sourceEndDate || event.extendedProps.sourceDate) >= visibleWeek.start)
      .sort((a,b) => (a.extendedProps.closingDate || a.extendedProps.sourceEndDate || a.start)
        .localeCompare(b.extendedProps.closingDate || b.extendedProps.sourceEndDate || b.start) || a.title.localeCompare(b.title,'de'));
    windowList.hidden = !windows.length;
    windowCount.textContent = `· ${windows.length}`;
    for (const event of windows.slice(0,8)) {
      const row = document.createElement('li'), link = document.createElement('a'), detail = document.createElement('small');
      link.href = event.url; link.textContent = event.title;
      bindEventLink(link,event);
      detail.textContent = `${event.extendedProps.course} · ${rangeText(event)}`;
      row.append(link,detail); windowItems.append(row);
    }
    if (windows.length > 8) {
      const row = document.createElement('li'), button = document.createElement('button');
      button.type='button'; button.className='secondary small'; button.textContent=`Alle ${windows.length} Zeiträume im Gantt ansehen →`;
      button.addEventListener('click',()=>chooseView('gantt')); row.append(button); windowItems.append(row);
    }
  }
  function renderGantt() {
    const count = Number(days.value), from = dayNumber(anchor), until = from + count;
    title.textContent = `${label(anchor, true)} – ${label(addDays(anchor,count-1), true)}`;
    dateInput.value = anchor;
    const visible = events.filter(event => {
      const p = event.extendedProps, start = dayNumber(p.sourceDate);
      const end = p.sourceEndDate ? dayNumber(p.sourceEndDate) + (p.sourceTime && p.sourceEndTime ? timePart(p.sourceEndTime) : 1) : start + 1;
      return end > from && start < until && (points.checked || (p.sourceEndDate && p.sourceEndDate > p.sourceDate));
    }).sort((a,b) => a.start.localeCompare(b.start) || a.title.localeCompare(b.title));
    const grid = document.createElement('div'); grid.className = 'gantt-grid'; grid.style.setProperty('--gantt-days',count);
    const heading = document.createElement('div'); heading.className = 'gantt-heading'; heading.textContent = `Einträge · ${visible.length}`; grid.append(heading);
    const dates = document.createElement('div'); dates.className = 'gantt-dates';
    for (let n=0; n<count; n++) {
      const date = addDays(anchor,n), cell = document.createElement('div');
      cell.className = 'gantt-day' + (date === today ? ' is-today' : '') + ([0,6].includes(new Date(date+'T12:00:00Z').getUTCDay()) ? ' is-weekend' : '');
      const strong = document.createElement('strong'); strong.textContent = label(date);
      const weekday = document.createElement('small'); weekday.textContent = new Intl.DateTimeFormat('de-AT',{weekday:'short',timeZone:'UTC'}).format(new Date(date+'T12:00:00Z'));
      cell.append(strong,weekday); dates.append(cell);
    }
    grid.append(dates);
    for (const event of visible) {
      const p = event.extendedProps, itemKind = event.classNames.find(c => c.startsWith('kind-'));
      const rowLabel = document.createElement('div'); rowLabel.className = 'gantt-label';
      const link = document.createElement('a'); link.href = event.url; link.textContent = event.title;
      bindEventLink(link,event);
      const detail = document.createElement('small'); detail.textContent = `${p.course} · ${p.kind}${p.status === 'done' ? ' · erledigt' : ''}`;
      rowLabel.append(link,detail); grid.append(rowLabel);
      const lane = document.createElement('div'); lane.className = 'gantt-lane';
      const start = dayNumber(p.sourceDate) + timePart(p.sourceTime);
      const multiDay = p.sourceEndDate && p.sourceEndDate > p.sourceDate;
      const timedSpan = p.sourceTime && p.sourceEndTime && p.sourceEndDate && (multiDay || p.sourceEndTime > p.sourceTime);
      const hasSpan = multiDay || timedSpan;
      const end = hasSpan ? dayNumber(p.sourceEndDate) + (timedSpan ? timePart(p.sourceEndTime) : 1) : start;
      const bar = document.createElement('a'); bar.href = event.url;
      bindEventLink(bar,event);
      bar.className = `gantt-bar ${itemKind} ${hasSpan ? '' : 'is-point'} ${p.status === 'done' ? 'is-done' : ''} ${p.conflict ? 'has-conflict' : ''}`;
      bar.style.setProperty('--calendar-course-color',p.courseColor || '#67836c');
      bar.style.left = `${Math.max(0,(start-from)/count*100)}%`;
      if (hasSpan) bar.style.width = `${Math.max(0,(Math.min(end,until)-Math.max(start,from))/count*100)}%`;
      bar.title = `${event.title} · ${rangeText(event)}${p.conflict ? ' · Widerspruch: Belege prüfen' : ''}${p.uncertainty ? ' · '+p.uncertainty : ''}`;
      bar.setAttribute('aria-label',bar.title); bar.textContent = hasSpan ? rangeText(event) : '◆';
      if (start < from) bar.classList.add('continues-before');
      if (end > until) bar.classList.add('continues-after');
      lane.append(bar);
      if (dayNumber(today) >= from && dayNumber(today) < until) { const now = document.createElement('span'); now.className = 'gantt-today'; now.style.left = `${(dayNumber(today)-from+.5)/count*100}%`; lane.append(now); }
      grid.append(lane);
    }
    gantt.replaceChildren(grid);
    if (!visible.length) { const empty = document.createElement('p'); empty.className = 'gantt-empty'; empty.textContent = 'Keine belegten Zeitspannen in diesem Zeitraum. Andere Tage/Kurse wählen oder Tages- und Einzeltermine einblenden.'; gantt.append(empty); }
  }
  const calendar = new FullCalendar.Calendar(element, {
    initialView:view === 'gantt' ? 'timeGridWeek' : view, initialDate:anchor, locale:'de', timeZone:'Europe/Vienna', firstDay:1,
    height:'auto', nowIndicator:true, headerToolbar:false, scrollTime:'08:00:00',
    slotMinTime:'08:00:00', slotMaxTime:'22:00:00',
    slotDuration:'00:30:00', slotHeaderInterval:'01:00:00', slotHeaderFormat:{hour:'2-digit',minute:'2-digit',hour12:false},
    slotHeaderDidMount: info => info.el.classList.add('calendar-hour'),
    eventTimeFormat:{hour:'2-digit',minute:'2-digit',hour12:false}, displayEventEnd:true, slotEventOverlap:false,
    eventMinHeight:26, dayMaxEvents:3,
    allDayText:'Ohne Uhrzeit', defaultTimedEventDuration:'00:01:00', editable:false,
    datesSet: info => { if (view !== 'gantt') { anchor = info.startStr.slice(0,10); title.textContent = view === 'dayGridMonth' ? info.view.title : `${label(anchor,true)} - ${label(addDays(info.endStr.slice(0,10),-1),true)}`; dateInput.value = anchor; visibleWeek = view === 'timeGridWeek' ? {start:info.startStr.slice(0,10),end:info.endStr.slice(0,10)} : null; renderAfterHours(calendarWeekEvents(events),visibleWeek); renderWindows(); } },
    events: async (info, success, failure) => { try { const data = await loadEvents(); success(view === 'timeGridWeek' ? calendarWeekEvents(data) : data); } catch(exception) { showError(exception); failure(exception); } },
    eventContent: info => {
      const p = info.event.extendedProps, node = document.createElement('div'); node.className='schedule-event';
      const clock = document.createElement('small'); clock.className='schedule-time';
      clock.textContent = (p.weekPoint ? (p.itemKind === 'registration' ? (p.closingDate ? 'Anmeldeschluss · ' : 'Anmeldung · ') : 'Frist · ') : '') + (info.timeText.replace(/\s*Uhr$/, '').replace('–', '–\u200b') || 'Ohne Uhrzeit')
        + (!info.event.allDay && !info.event.end && !p.weekPoint ? ' · Ende offen' : '');
      const name = document.createElement('strong'); name.textContent=calendarShortLabel(info.event.title);
      const courseName = document.createElement('small'); courseName.className='schedule-course'; courseName.textContent=calendarShortLabel(p.course,36);
      node.append(clock,name);
      if (!name.textContent.includes(courseName.textContent)) node.append(courseName);
      return {domNodes:[node]};
    },
    eventClick: info => {
      if (info.jsEvent.ctrlKey || info.jsEvent.metaKey || info.jsEvent.shiftKey || info.jsEvent.altKey) return;
      info.jsEvent.preventDefault(); showEvent(info.event,info.el);
    },
    eventDidMount: info => {
      const p = info.event.extendedProps;
      info.el.title = `${info.event.title} · ${(p.tags||[]).map(t=>t.label).join(' · ')} · ${rangeText(info.event)}${p.closingDate ? ' · '+(p.itemKind==='registration' ? 'Anmeldeschluss' : 'Fensterschluss')+': '+label(p.closingDate,true)+(p.closingTime ? ' '+p.closingTime.slice(0,5) : ' · ohne Uhrzeit') : ''}`;
      const color = p.courseColor || '#67836c';
      info.el.classList.add('calendar-entry');
      if(p.status === 'done') info.el.classList.add('calendar-entry-done');
      info.el.style.setProperty('--calendar-course-color',color);
      info.el.style.setProperty('--fc-event-color',color); info.el.style.setProperty('--fc-event-contrast-color','#253e36');
      info.el.setAttribute('aria-label',info.el.title); info.el.setAttribute('aria-haspopup','dialog');
      if(p.conflict) { info.el.style.outline = '2px dashed #c48722'; info.el.style.outlineOffset = '1px'; }
    }
  });
  function chooseView(next) {
    view = next;
    buttons.forEach(button => button.setAttribute('aria-pressed',String(button.dataset.calendarView === view)));
    document.querySelector('#calendar-surface').hidden = view === 'gantt'; gantt.hidden = view !== 'gantt'; options.hidden = view !== 'gantt';
    if(view!=='timeGridWeek')renderAfterHours(events,null);
    renderWindows();
    element.dataset.view = view;
    explanation.textContent = view === 'gantt' ? 'Balken zeigen belegte Zeiträume. Tages- und Einzeltermine optional: ◇ bedeutet nur Termin/Frist. Klick öffnet das Termindetail.' : view === 'timeGridWeek' ? 'Stundenplan 08–22 Uhr · Europe/Vienna. Persönliche Termine und Fristpunkte; Zeiträume und Anmeldefenster separat darunter. Ohne Uhrzeit bleibt im Datumsbereich, ohne Endzeit bleibt die Dauer offen.' : 'Angaben ohne Uhrzeit bleiben reine Datumswerte. Klick öffnet das Termindetail.';
    try { localStorage.setItem('studycenter_ice.calendar.view',view); } catch (_) {}
    if (view === 'gantt') { renderGantt(); loadEvents().catch(showError); }
    else { calendar.changeView(view,anchor); if (viewChosen) calendar.refetchEvents(); }
    viewChosen = true;
  }
  calendar.render(); chooseView(view);
  buttons.forEach(button => button.addEventListener('click',()=>chooseView(button.dataset.calendarView)));
  function refresh() { if (view === 'gantt') loadEvents().catch(showError); else { afterHours.hidden=true; calendar.refetchEvents(); } }
  [course,history,kind].forEach(control=>{
    control.addEventListener('change',syncFilterUrl);
    control.addEventListener('change',refresh);
  });
  [days,points].forEach(control=>control.addEventListener('change',renderGantt));
  document.querySelector('#calendar-prev').addEventListener('click',()=>{ if(view==='gantt'){anchor=addDays(anchor,-Number(days.value));renderGantt();} else calendar.prev(); });
  document.querySelector('#calendar-next').addEventListener('click',()=>{ if(view==='gantt'){anchor=addDays(anchor,Number(days.value));renderGantt();} else calendar.next(); });
  document.querySelector('#calendar-today').addEventListener('click',()=>{anchor=today;if(view==='gantt')renderGantt();else calendar.gotoDate(today);});
  dateInput.addEventListener('change',()=>{if(!dateInput.value)return;anchor=dateInput.value;if(view==='gantt')renderGantt();else calendar.gotoDate(anchor);});
});
