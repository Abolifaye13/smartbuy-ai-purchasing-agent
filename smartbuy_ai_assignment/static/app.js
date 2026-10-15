const $ = id => document.getElementById(id);
let lastDecision = null;
const money = n => "₹" + Number(n).toLocaleString("en-IN");
const number = n => Number(n).toLocaleString("en-IN");
const esc = s => String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(url, options={}) {
  const r=await fetch(url,{headers:{"Content-Type":"application/json"},...options});
  const d=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.detail||"Request failed");
  return d;
}
function alertMsg(msg,ok=false){$("alert").textContent=msg;$("alert").className="alert"+(ok?" ok":"");}
function hideAlert(){$("alert").className="alert hidden";}
async function loadScenario(){
 const s=await api("/api/scenario");
 const facts=[
  ["On-hand inventory",number(s.on_hand),"units available"],
  ["Forecast demand",number(s.forecast_demand),`${s.horizon_days}-day horizon`],
  ["Safety stock",number(s.safety_stock),"units buffer"],
  ["Open PO supply",number(s.open_po_units),s.open_po_id],
  ["Supplier lead time",`${s.supplier_lead_time_days} days`,s.supplier_name],
  ["Supplier MOQ",number(s.supplier_moq),"units / order multiple"],
  ["Supplier capacity",number(s.supplier_capacity),"units available"],
  ["Budget available",money(s.budget_available),"purchasing budget"],
  ["Free storage",number(s.storage_capacity_free),"unit capacity"],
  ["Unit cost",money(s.unit_cost),"per unit"],
 ];
 $("facts").innerHTML=facts.map(([k,v,n])=>`<div class="fact"><span>${esc(k)}</span><strong>${esc(v)}</strong><small>${esc(n)}</small></div>`).join("");
 $("budgetChange").value=s.budget_available;$("storageChange").value=s.storage_capacity_free;
}
$("investigate").addEventListener("click",async()=>{
 hideAlert();$("investigate").disabled=true;
 try{
  const d=await api("/api/investigate",{method:"POST",body:JSON.stringify({sku:"SKU-APPLE-1",proposed_quantity:Number($("proposed").value)})});
  lastDecision=d;renderDecision(d);$("executePanel").classList.add("hidden");$("result").classList.add("hidden");
 }catch(e){alertMsg(e.message)}finally{$("investigate").disabled=false}
});
function renderDecision(d){
 $("decision").classList.remove("hidden");
 const c=d.calculation||{}, s=d.evidence||{};
 const constraints=(d.constraints||[]).map(x=>`<div class="constraint"><b>${esc(x.constraint.replaceAll("_"," "))}</b><br>${esc(x.impact)}. Limit: ${esc(x.limit)}; desired: ${esc(x.desired)}.</div>`).join("")||'<p class="muted">No hard constraint was violated by the recommended quantity.</p>';
 $("decision").innerHTML=`<div class="decision-head"><div><p class="kicker">AGENT DECISION</p><h2>${esc(d.decision)} the recommendation</h2></div><span class="decision-badge ${esc(d.decision)}">${esc(d.decision)}</span></div>
 <p class="reason">${esc(d.reason)}</p>
 <div class="metric-grid">
 <div class="metric"><span>System proposed</span><strong>${number(d.proposed_quantity??0)}</strong></div>
 <div class="metric"><span>Calculated net need</span><strong>${number(c.net_need_after_inventory_and_open_po??0)}</strong></div>
 <div class="metric"><span>Agent action quantity</span><strong>${number(d.recommended_quantity??0)}</strong></div>
 <div class="metric"><span>Expected order value</span><strong>${money(c.recommended_total_cost??0)}</strong></div>
 <div class="metric"><span>Budget unit limit</span><strong>${number(c.max_by_budget??0)}</strong></div>
 <div class="metric"><span>Uncovered net need</span><strong>${number(c.remaining_coverage_gap??0)}</strong></div>
 </div>
 <div class="split"><div><h3>Evidence considered</h3><ul class="list">${(d.explanation||[]).map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div><div><h3>Constraint analysis</h3>${constraints}</div></div>
 <h3 style="margin-top:20px">Calculation</h3><div class="evidence">
 ${Object.entries(c).map(([k,v])=>`<div class="evidence-item"><span>${esc(k.replaceAll("_"," "))}</span><b>${number(v)}</b></div>`).join("")}
 </div>`;
 const canExecute=Number(d.recommended_quantity)>0;
 $("executePanel").classList.toggle("hidden",!canExecute);
 $("executeSummary").innerHTML=`Agent recommends <b>${number(d.recommended_quantity||0)} units</b> from ${esc(s.supplier_name)} at ${money(s.unit_cost)} per unit (estimated total <b>${money(c.recommended_total_cost||0)}</b>). The API will recalculate current constraints before creating the PO.`;
 $("decision").scrollIntoView({behavior:"smooth",block:"start"});
}
$("execute").addEventListener("click",async()=>{
 if(!lastDecision||!lastDecision.recommended_quantity)return;
 $("execute").disabled=true;hideAlert();
 try{
  const s=lastDecision.evidence;
  const d=await api("/api/execute",{method:"POST",body:JSON.stringify({
   sku:s.sku,node:s.node,supplier_id:s.supplier_id,quantity:lastDecision.recommended_quantity,approved:true
  })});
  renderResult(d);alertMsg("Purchase order action completed and post-action validation passed.",true);await refreshAll();
 }catch(e){alertMsg(e.message+" Please investigate again before retrying.");}
 finally{$("execute").disabled=false}
});
function renderResult(d){
 const p=d.purchase_order,v=d.validation;
 $("result").classList.remove("hidden");
 $("result").innerHTML=`<p class="kicker">POST-ACTION FEEDBACK</p><h2>Purchase order #${p.id} ${v.passed?"validated":"flagged"}</h2><p class="reason">${esc(d.message)} ${esc(d.note)}</p>
 <div class="metric-grid"><div class="metric"><span>PO quantity</span><strong>${number(p.quantity)}</strong></div><div class="metric"><span>Unit cost</span><strong>${money(p.unit_cost)}</strong></div><div class="metric"><span>Total cost</span><strong>${money(p.total_cost)}</strong></div></div>
 <h3>Validation checks</h3><div class="validation">${Object.entries(v.checks).map(([k,ok])=>`<div class="check"><b class="${ok?"pass":"fail"}">${ok?"✓ PASS":"✕ FAIL"}</b>${esc(k.replaceAll("_"," "))}</div>`).join("")}</div>`;
 $("result").scrollIntoView({behavior:"smooth",block:"center"});
}
$("mutate").addEventListener("click",async()=>{
 try{
  const d=await api("/api/dev/mutate-scenario",{method:"POST",body:JSON.stringify({budget_available:Number($("budgetChange").value),storage_capacity_free:Number($("storageChange").value)})});
  alertMsg("Mock purchasing constraints updated. Re-investigate to get a fresh decision.",true);await loadScenario();await refreshAll();
 }catch(e){alertMsg(e.message)}
});
$("reset").addEventListener("click",async()=>{
 try{await api("/api/dev/reset",{method:"POST"});lastDecision=null;$("decision").classList.add("hidden");$("executePanel").classList.add("hidden");$("result").classList.add("hidden");hideAlert();await loadScenario();await refreshAll();alertMsg("Scenario reset to defaults.",true)}catch(e){alertMsg(e.message)}
});
async function refreshOrders(){
 const d=await api("/api/purchase-orders");
 $("orders").innerHTML=d.purchase_orders.length?d.purchase_orders.map(p=>`<div class="record"><div><strong>#${p.id} · ${esc(p.sku)} · ${number(p.quantity)} units</strong><span class="muted">${esc(p.node)} · ${new Date(p.created_at).toLocaleString()}</span></div><div class="right"><strong>${money(p.total_cost)}</strong><span class="status">${esc(p.status)}</span></div></div>`).join(""):'<p class="muted">No purchase orders yet.</p>';
}
async function refreshAudit(){
 const d=await api("/api/audit");
 $("audit").innerHTML=d.events.length?d.events.map(e=>`<div class="event"><b>${esc(e.event)}</b> · ${new Date(e.created_at).toLocaleTimeString()}<br><span class="muted">${esc(JSON.stringify(e.details))}</span></div>`).join(""):'<p class="muted">No audit events yet.</p>';
}
async function refreshAll(){try{await Promise.all([refreshOrders(),refreshAudit()])}catch(e){console.warn(e)}}
$("refresh").addEventListener("click",refreshOrders);$("auditRefresh").addEventListener("click",refreshAudit);
loadScenario().then(refreshAll).catch(e=>alertMsg(e.message));
