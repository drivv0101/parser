/* Projects, purchasing controls, favorites and export. Data stays in this browser. */
(() => {
  const esc = escapeHtml;
  const units = ['уп','кг','л','м','м²','м³'];
  const load = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; } };
  const style = document.createElement('style');
  style.textContent = `
    .tools {display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
    .tools button,.tools select,.tools input,dialog button,dialog input,dialog select,.est-item-row select {font:inherit;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);max-width:100%}
    .tools input[type=number] {width:105px}.tools label{font-size:12px;color:var(--muted)}
    .tools button,dialog button{cursor:pointer}.tools select{max-width:220px}
    dialog{border:1px solid var(--border);border-radius:16px;background:var(--surface);color:var(--text);width:min(850px,95vw);max-height:85vh;overflow:auto}
    dialog::backdrop{background:#0008} dialog table{width:100%;border-collapse:collapse}dialog td,dialog th{padding:7px;text-align:left;border-bottom:1px solid var(--border)}
    dialog input{width:100%}.review-warning{color:var(--accent);font-size:12px}.status-row{font-size:12px;margin:5px 0;color:var(--muted)}
    .small-action{border:0;background:none;color:var(--accent);font:inherit;font-size:12px;cursor:pointer;text-align:left;padding:4px}
    @media(max-width:650px){.wrap{padding:24px 12px}.top-row{flex-wrap:wrap}.search-row{flex-wrap:wrap}.search-input-wrap{min-width:0;width:100%}#q{min-width:0;width:100%}.row,.est-line{flex-wrap:wrap;padding:16px}.price-block{min-width:90px}.cta-row{flex-direction:row;flex-wrap:wrap}.meta-line{flex-wrap:wrap}.est-item-row{flex-wrap:wrap}.est-item-name{flex-basis:100%}.store-option{flex-wrap:wrap}.discounts-panel{right:0}.estimate-hint{min-width:0}}
    @media print{body{background:white!important;color:black!important}.wrap{max-width:none;padding:0}.top-row,.subtitle,.tabs,#panel-search,.footer-note,#estimateInput,.estimate-hint,#addToEstimateList,#estimateListBox,.tools,#statusBox,.est-line-edit{display:none!important}#panel-estimate{display:block!important}.results-card,.summary-card{box-shadow:none;border:1px solid #ccc;background:white!important;color:black!important}.est-line{break-inside:avoid}.summary-card span{color:black!important}.name-link{color:black!important}@page{margin:15mm}}
  `;
  document.head.append(style);
  const dialog = document.createElement('dialog'); document.body.append(dialog);
  function show(title, body) {
    dialog.innerHTML = `<div class="tools"><strong>${esc(title)}</strong><button id="closeDialog" style="margin-left:auto">Закрыть</button></div>${body}`;
    dialog.querySelector('#closeDialog').onclick = () => dialog.close();
    if (!dialog.open) dialog.showModal();
  }
  function error(message) { show('Не удалось выполнить действие', `<p>${esc(message)}</p>`); }

  // Named estimates: migrate the existing list without losing it.
  const BOOK_KEY = 'stroyceny-estimates-v2';
  let book = load(BOOK_KEY, null);
  if (!book || !Array.isArray(book.projects) || !book.projects.length) {
    book = {active:'first',projects:[{id:'first',name:'Моя смета',items:estimateList,delivery:{}}]};
  }
  let project = book.projects.find(p => p.id === book.active) || book.projects[0];
  book.active = project.id;
  estimateList = (project.items || []).map(normalizeItem).filter(Boolean);
  const oldSave = saveEstimateList;
  function saveBook() { project.items = estimateList; localStorage.setItem(BOOK_KEY, JSON.stringify(book)); }
  saveEstimateList = function() { oldSave(); saveBook(); };
  window.getDelivery = () => project.delivery || {};
  const projectTools = document.createElement('div'); projectTools.className='tools';
  document.getElementById('panel-estimate').prepend(projectTools);
  function renderProjects() {
    projectTools.innerHTML=`<label>Смета <select id="projectSelect">${book.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===project.id?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label><input id="projectName" aria-label="Название сметы" value="${esc(project.name)}" maxlength="100"><button id="newProject">Новая</button><button id="deleteProject">Удалить</button><button id="exportXlsx">Excel</button><button id="exportPdf">PDF / печать</button><span style="font-size:12px">Сметы сохраняются в этом браузере</span>`;
    projectTools.querySelector('#projectSelect').onchange=e=>{saveBook();project=book.projects.find(p=>p.id===e.target.value);book.active=project.id;estimateList=(project.items||[]).map(normalizeItem).filter(Boolean);saveEstimateList();renderProjects();renderDelivery();renderEstimateListBox();calcEstimate();};
    projectTools.querySelector('#projectName').onchange=e=>{project.name=e.target.value.trim()||'Без названия';saveBook();renderProjects();};
    projectTools.querySelector('#newProject').onclick=()=>{saveBook();project={id:crypto.randomUUID(),name:'Новая смета',items:[],delivery:{}};book.projects.push(project);book.active=project.id;estimateList=[];saveEstimateList();renderProjects();renderDelivery();renderEstimateListBox();calcEstimate();};
    projectTools.querySelector('#deleteProject').onclick=()=>{
      show('Удалить смету?',`<p>${esc(project.name)}: ${estimateList.length} позиций. Это действие удалит сохранённый список.</p><button id="confirmDelete">Удалить смету</button>`);
      dialog.querySelector('#confirmDelete').onclick=()=>{book.projects=book.projects.filter(p=>p.id!==project.id);if(!book.projects.length)book.projects=[{id:crypto.randomUUID(),name:'Моя смета',items:[],delivery:{}}];project=book.projects[0];book.active=project.id;estimateList=(project.items||[]).map(normalizeItem).filter(Boolean);saveEstimateList();renderProjects();renderDelivery();renderEstimateListBox();calcEstimate();dialog.close();};
    };
    projectTools.querySelector('#exportXlsx').onclick=async()=>{
      if(!estimateList.length)return error('Сначала добавьте позиции.');
      try{const resp=await fetch('/api/export/xlsx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(requestData())});if(!resp.ok)throw Error('Ошибка экспорта: '+resp.status);const url=URL.createObjectURL(await resp.blob());const a=document.createElement('a');a.href=url;a.download=project.name.replace(/[\\/:*?"<>|]/g,'_')+'.xlsx';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){error(e.message);}
    };
    projectTools.querySelector('#exportPdf').onclick=async()=>{
      if(!estimateList.length)return error('Сначала добавьте позиции.');
      await calcEstimate(); if(!window.lastEstimateData)return error('Дождитесь успешного расчёта.');
      const title=document.title;document.title=project.name;window.print();document.title=title;
    };
  }
  function requestData(){return {items:estimateList.map(it=>({query:it.query,qty:it.qty,qty_unit:it.qty_unit||'уп',url:it.url||null})),discounts:discountsMap,delivery:window.getDelivery()};}
  const deliveryTools=document.createElement('details'); deliveryTools.innerHTML='<summary>Доставка по магазинам</summary><div class="tools" id="deliveryInputs"></div><p class="estimate-hint">Укажите стоимость одной доставки из каждого магазина. Она добавляется один раз на магазин. 0 ₽ означает самовывоз или бесплатную доставку.</p>';
  projectTools.after(deliveryTools);
  async function renderDelivery(){const stores=await storesReady;const box=deliveryTools.querySelector('#deliveryInputs');box.innerHTML=stores.map(s=>`<label>${esc(s.name)} <input type="number" min="0" max="1000000" step="0.01" value="${Number((project.delivery||{})[s.slug])||0}" data-delivery="${esc(s.slug)}"> ₽</label>`).join('');box.onchange=e=>{if(!e.target.dataset.delivery)return;const value=Number(e.target.value);if(!Number.isFinite(value)||value<0||value>1000000)return;project.delivery=project.delivery||{};project.delivery[e.target.dataset.delivery]=value;saveBook();calcEstimate();};}
  renderProjects();renderDelivery();saveEstimateList();renderEstimateListBox();calcEstimate();

  // Always review parsed text. Ambiguous weights keep the original pack as an alternative.
  window.previewParsedLines = parsed => {
    show('Проверьте позиции перед добавлением',`<p>«уп» — упаковки или штуки. Для кг, м² и метров рассчитаем целое количество упаковок и остаток.</p><table><thead><tr><th>Материал</th><th>Количество</th><th>Единица</th><th></th></tr></thead><tbody>${parsed.map((p,i)=>`<tr data-line="${i}"><td><input data-field="query" value="${esc(p.query)}" maxlength="200">${p.needs_review?'<div class="review-warning">Это потребность или фасовка одного товара?</div>':''}</td><td><input data-field="qty" type="number" min="0.001" max="1000000" step="any" value="${p.qty}"></td><td><select data-field="unit">${units.map(u=>`<option ${u===p.qty_unit?'selected':''}>${u}</option>`).join('')}</select></td><td>${p.needs_review?`<button data-as-pack="${i}">Это одна упаковка ${p.qty} ${esc(p.qty_unit)}</button>`:''}</td></tr>`).join('')}</tbody></table><div class="tools"><button id="acceptLines">Добавить в смету</button><span id="parseError" role="alert"></span></div>`);
    dialog.querySelectorAll('[data-as-pack]').forEach(btn=>btn.onclick=()=>{const p=parsed[Number(btn.dataset.asPack)];const row=btn.closest('tr');row.querySelector('[data-field=query]').value=p.query+' '+p.qty+' '+p.qty_unit;row.querySelector('[data-field=qty]').value=1;row.querySelector('[data-field=unit]').value='уп';btn.remove();});
    dialog.querySelector('#acceptLines').onclick=()=>{const selected=[...dialog.querySelectorAll('[data-line]')].map(row=>({query:row.querySelector('[data-field=query]').value.trim(),qty:Number(row.querySelector('[data-field=qty]').value),qty_unit:row.querySelector('[data-field=unit]').value}));if(estimateList.length+selected.length>200||selected.some(p=>!p.query||!Number.isFinite(p.qty)||p.qty<=0||p.qty>1000000)){dialog.querySelector('#parseError').textContent='Проверьте названия и количества; максимум 200 позиций.';return;}acceptParsedLines(selected);dialog.close();};
  };

  // Search filters and paging.
  let offset=0;
  const filters=document.createElement('details');filters.innerHTML=`<summary>Фильтры и сортировка</summary><div class="tools"><select id="filterStore" aria-label="Магазин"><option value="">Все магазины</option></select><select id="filterCategory" aria-label="Категория"><option value="">Все категории</option></select><select id="filterUnit" aria-label="Фасовка"><option value="">Все единицы</option>${units.slice(1).map(u=>`<option>${u}</option>`).join('')}</select><label><input type="checkbox" id="filterStock"> Только в наличии</label><label><input type="checkbox" id="filterExact"> Все слова запроса</label><input id="filterMin" type="number" min="0" placeholder="Цена от" aria-label="Цена от"><input id="filterMax" type="number" min="0" placeholder="Цена до" aria-label="Цена до"><select id="filterSort" aria-label="Сортировка"><option value="relevance">По соответствию</option><option value="price">По цене упаковки</option><option value="unit_price">По цене за единицу</option></select></div><p class="estimate-hint">Укажите марку, сорт, толщину и бренд в запросе. Для сравнения цены за единицу выберите кг, м² или другую единицу.</p>`;
  document.getElementById('panel-search').insertBefore(filters,results);
  storesReady.then(stores=>{filters.querySelector('#filterStore').innerHTML= '<option value="">Все магазины</option>'+stores.map(s=>`<option value="${esc(s.slug)}">${esc(s.name)}</option>`).join('');});
  fetch('/api/categories').then(r=>r.ok?r.json():[]).then(cats=>{filters.querySelector('#filterCategory').innerHTML='<option value="">Все категории</option>'+cats.map(c=>`<option>${esc(c)}</option>`).join('');}).catch(()=>{});
  filters.onchange=()=>{offset=0;if(input.value.trim())search();};input.addEventListener('input',()=>{offset=0;});
  window.appendSearchFilters=params=>{for(const [id,key]of [['filterStore','store'],['filterCategory','category'],['filterUnit','pack_unit'],['filterMin','min_price'],['filterMax','max_price'],['filterSort','sort']]){const value=filters.querySelector('#'+id).value;if(value!=='')params.set(key,value);}if(filters.querySelector('#filterStock').checked)params.set('stock_only','true');if(filters.querySelector('#filterExact').checked)params.set('exact','true');params.set('offset',offset);};
  const paging=document.createElement('div');paging.className='tools';results.after(paging);
  window.searchPageInfo=data=>{paging.innerHTML=`<button id="previousPage" ${offset===0?'disabled':''}>Назад</button><span>${data.total||0} основных совпадений · страница ${Math.floor(offset/100)+1}</span><button id="nextPage" ${data.has_more?'':'disabled'}>Далее</button>`;paging.querySelector('#previousPage').onclick=()=>{offset=Math.max(0,offset-100);search();};paging.querySelector('#nextPage').onclick=()=>{offset+=100;search();};};

  // Favorites and history.
  const FAV_KEY='stroyceny-favorites';let favorites=load(FAV_KEY,[]);if(!Array.isArray(favorites))favorites=[];
  const favButton=document.createElement('button');favButton.textContent='Избранное';favButton.className='cta-ghost';filters.before(favButton);
  function saveFavorites(){localStorage.setItem(FAV_KEY,JSON.stringify(favorites));}
  async function history(url){show('История цены','<p>Загружаю…</p>');try{const resp=await fetch('/api/price-history?'+new URLSearchParams({url}));if(!resp.ok)throw Error('Товар не найден');const data=await resp.json();show(data.name,`<p>Цены магазина без личной скидки. История накапливается с момента включения функции.</p>${data.history.length===1?'<p>Пока сохранена только одна цена.</p>':''}<table><tr><th>Дата</th><th>Цена</th></tr>${data.history.map(h=>`<tr><td>${esc(formatDate(h.date))}</td><td>${fmtMoney(h.price)} ₽</td></tr>`).join('')}</table>`);}catch(e){error(e.message);}}
  function renderFavorites(){show('Избранное',favorites.length?favorites.map((f,i)=>`<div class="tools"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name)}</a><button data-fav-add="${i}">В смету</button><button data-fav-history="${i}">История цены</button><button data-fav-remove="${i}">Убрать</button></div>`).join(''):'<p>Добавляйте товары кнопкой ☆ в результатах поиска.</p>');dialog.querySelectorAll('[data-fav-add]').forEach(b=>b.onclick=()=>{addPinnedToEstimate(favorites[Number(b.dataset.favAdd)]);b.textContent='Добавлено';});dialog.querySelectorAll('[data-fav-history]').forEach(b=>b.onclick=()=>history(favorites[Number(b.dataset.favHistory)].url));dialog.querySelectorAll('[data-fav-remove]').forEach(b=>b.onclick=()=>{favorites.splice(Number(b.dataset.favRemove),1);saveFavorites();renderFavorites();});}
  favButton.onclick=renderFavorites;
  new MutationObserver(()=>{results.querySelectorAll('[data-add-to-estimate]').forEach(btn=>{if(btn.dataset.enhanced)return;btn.dataset.enhanced='1';const product={name:btn.dataset.name,url:btn.dataset.url,store:btn.dataset.store};const star=document.createElement('button');star.className='small-action';const refresh=()=>{star.textContent=favorites.some(f=>f.url===product.url)?'★ В избранном':'☆ В избранное';};refresh();star.onclick=()=>{if(favorites.some(f=>f.url===product.url))favorites=favorites.filter(f=>f.url!==product.url);else favorites.push(product);saveFavorites();refresh();};const hist=document.createElement('button');hist.className='small-action';hist.textContent='История цены';hist.onclick=()=>history(product.url);btn.after(star,hist);});}).observe(results,{childList:true,subtree:true});

  // Status polling is read-only; no new scrape is started by opening this page.
  const status=document.createElement('details');status.id='statusBox';status.innerHTML='<summary>Обновление каталогов</summary><div></div>';document.querySelector('.footer-note').before(status);
  async function refreshStatus(){try{const resp=await fetch('/api/scrape-status');if(!resp.ok)throw Error();const runs=await resp.json();const labels={running:'Сбор идёт',success:'Завершён',partial:'Неполный сбор — прежние товары сохранены',failed:'Ошибка',unknown:'Нет данных о запуске',interrupted:'Прерван'};status.querySelector('div').innerHTML=runs.map(r=>`<div class="status-row">${esc(knownStores.find(s=>s.slug===r.store_slug)?.name||r.store_slug)}: ${esc(labels[r.status]||r.status)} · ${r.done}/${r.total} категорий · ${r.products} товаров${r.finished_at?' · '+esc(formatDate(r.finished_at)):''}${r.message?'<br>'+esc(r.message):''}</div>`).join('');const storesResp=await fetch('/api/stores');if(storesResp.ok)renderStoreFreshness(await storesResp.json());}catch{status.querySelector('div').textContent='Не удалось получить состояние обновления.';}}
  refreshStatus();setInterval(()=>{if(!document.hidden)refreshStatus();},30000);
})();
