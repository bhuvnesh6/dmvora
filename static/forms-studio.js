/* DMvora Forms Studio extension. Load after app.js. */
(() => {
  const $ = id => document.getElementById(id);
  let studioFields = [];
  const fieldTypes = [
    ['text','Short text'],['email','Email'],['phone','Phone'],['number','Number'],
    ['date','Date'],['dropdown','Dropdown'],['radio','Single choice'],['checkbox','Multiple choice']
  ];
  const escHtml = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function fieldCard(f,i){
    const choices = ['dropdown','radio','checkbox'].includes(f.type);
    return `<article class="studio-field" data-index="${i}">
      <div class="studio-field-top"><span class="studio-drag">⠿</span><b>${i+1}. ${escHtml(f.label||'Untitled field')}</b>
      <button type="button" class="studio-remove" data-remove="${i}" aria-label="Remove field">×</button></div>
      <div class="studio-field-grid">
        <label>Field label<input data-prop="label" data-i="${i}" value="${escHtml(f.label)}" placeholder="e.g. Full name"></label>
        <label>Field type<select data-prop="type" data-i="${i}">${fieldTypes.map(([v,l])=>`<option value="${v}" ${f.type===v?'selected':''}>${l}</option>`).join('')}</select></label>
      </div>
      ${choices?`<label class="studio-options-label">Options <small>One option per line (2–20)</small><textarea data-prop="options" data-i="${i}" rows="3">${escHtml((f.options||[]).join('\n'))}</textarea></label>`:''}
      <label class="studio-required"><input type="checkbox" data-prop="required" data-i="${i}" ${f.required?'checked':''}> Required field</label>
    </article>`;
  }
  function renderStudioFields(){
    const host=$('studioFieldList'); if(!host)return;
    host.innerHTML=studioFields.map(fieldCard).join('') || '<div class="studio-empty">No fields yet. Add your first field to start designing the WhatsApp form.</div>';
  }
  function openStudio(){
    if(!window.selectedNumber && !$('numberSelect')?.value){ if(window.toast)toast('Connect and select a WhatsApp number first'); return; }
    studioFields=[]; $('studioFormName').value='New lead form'; $('studioScreenTitle').value='A few details';
    $('studioPrompt').value='Please share a few details so we can help you.'; $('studioCta').value='Continue';
    $('studioCategory').value='LEAD_GENERATION'; $('formsEditor').classList.remove('hidden'); $('formsListWrap').classList.add('hidden'); renderStudioFields();
  }
  async function loadStudioForms(){
    const host=$('studioFormsList'); if(!host)return;
    try{
      const numberId=$('numberSelect')?.value||'';
      const d=await api('/api/forms?number_id='+encodeURIComponent(numberId));
      host.innerHTML=(d.forms||[]).map(f=>`<article class="studio-form-card">
        <div class="studio-form-card-head"><div><h3>${escHtml(f.name)}</h3><p>${(f.fields||[]).length} fields · ${escHtml(f.category||'LEAD_GENERATION')}</p></div><span class="studio-status ${String(f.status).toLowerCase()}">${escHtml(f.status||'LOCAL')}</span></div>
        <small>Meta Flow ID: ${escHtml(f.meta_flow_id||'Not created')}</small>
        <div class="studio-form-actions">${f.status==='DRAFT'?`<button class="btn primary" data-publish="${f.id}">Publish to WhatsApp</button>`:''}<button class="btn light" data-copy="${escHtml(f.meta_flow_id||'')}" ${f.meta_flow_id?'':'disabled'}>Copy Flow ID</button>${f.status!=='PUBLISHED'?`<button class="btn light" data-delete-form="${f.id}">Delete draft</button>`:''}</div>
      </article>`).join('') || '<div class="studio-empty">No forms yet. Create your first native WhatsApp form.</div>';
    }catch(e){host.innerHTML=`<div class="studio-empty">${escHtml(e.message)}</div>`;}
  }
  async function saveStudioForm(){
    const button=$('studioCreateBtn'); button.disabled=true; button.textContent='Creating in Meta…';
    try{
      const d=await api('/api/forms',{method:'POST',body:JSON.stringify({
        number_id:$('numberSelect').value,name:$('studioFormName').value.trim(),screen_title:$('studioScreenTitle').value.trim(),
        prompt:$('studioPrompt').value.trim(),cta_text:$('studioCta').value.trim(),category:$('studioCategory').value,
        fields:studioFields
      })});
      toast(`Draft created in Meta · Flow ${d.meta_flow_id}`); $('formsEditor').classList.add('hidden'); $('formsListWrap').classList.remove('hidden'); await loadStudioForms();
    }catch(e){toast(e.message); if(e.message)console.error(e);}finally{button.disabled=false;button.textContent='Create WhatsApp Flow';}
  }
  document.addEventListener('click',async e=>{
    const add=e.target.closest('[data-add-field]'); if(add){studioFields.push({label:'New field',type:add.dataset.addField||'text',required:false,options:['Option 1','Option 2']});renderStudioFields();return;}
    const remove=e.target.closest('[data-remove]'); if(remove){studioFields.splice(Number(remove.dataset.remove),1);renderStudioFields();return;}
    const publish=e.target.closest('[data-publish]'); if(publish){publish.disabled=true;publish.textContent='Publishing…';try{await api(`/api/forms/${publish.dataset.publish}/publish`,{method:'POST'});toast('WhatsApp Flow published');await loadStudioForms();}catch(err){toast(err.message);publish.disabled=false;publish.textContent='Publish to WhatsApp';}return;}
    const copy=e.target.closest('[data-copy]'); if(copy && copy.dataset.copy){await navigator.clipboard.writeText(copy.dataset.copy);toast('Flow ID copied');return;}
    const del=e.target.closest('[data-delete-form]'); if(del){if(!confirm('Delete this unpublished form and its Meta draft?'))return;try{await api(`/api/forms/${del.dataset.deleteForm}`,{method:'DELETE'});toast('Draft deleted');await loadStudioForms();}catch(err){toast(err.message);}return;}
  });
  document.addEventListener('input',e=>{
    const i=Number(e.target.dataset.i), prop=e.target.dataset.prop; if(!Number.isInteger(i)||!prop||!studioFields[i])return;
    if(prop==='options')studioFields[i].options=e.target.value.split('\n').map(x=>x.trim()).filter(Boolean);
    else studioFields[i][prop]=e.target.value;
    if(prop==='label'){const title=e.target.closest('.studio-field')?.querySelector('.studio-field-top b');if(title)title.textContent=`${i+1}. ${e.target.value||'Untitled field'}`;}
  });
  document.addEventListener('change',e=>{
    const i=Number(e.target.dataset.i), prop=e.target.dataset.prop; if(!Number.isInteger(i)||!prop||!studioFields[i])return;
    studioFields[i][prop]=prop==='required'?e.target.checked:e.target.value;
    if(prop==='type')renderStudioFields();
  });
  window.addFormField=openStudio;
  window.loadForms=loadStudioForms;
  window.closeStudioForms=()=>{$('formsEditor').classList.add('hidden');$('formsListWrap').classList.remove('hidden');};
  window.saveStudioForm=saveStudioForm;
  window.loadFormOptions=async function(){try{const d=await api('/api/forms?number_id='+encodeURIComponent($('numberSelect')?.value||''));document.querySelectorAll('.form-choice').forEach(sel=>{const old=sel.value;sel.innerHTML='<option value="">Choose a saved WhatsApp Flow</option>'+(d.forms||[]).filter(f=>f.status==='PUBLISHED').map(f=>`<option value="${f.id}">${escHtml(f.name)}</option>`).join('');sel.value=old;});}catch(e){console.warn(e);}};
  document.addEventListener('DOMContentLoaded',()=>{loadStudioForms();});
})();
