import {createClient} from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";
const URL="https://nzuzpknflhqdiqyxmjto.supabase.co";
const KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im56dXpwa25mbGhxZGlxeXhtanRvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQwNDc2NzYsImV4cCI6MjA5OTYyMzY3Nn0.1vTTF9-7f0wvvqMYi7m5GDgLbrT3kOJwdtVkybAtpdo";
const sb=createClient(URL,KEY,{auth:{persistSession:true,autoRefreshToken:true},realtime:{params:{eventsPerSecond:6}}});
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const st={channels:[],videos:[],events:[],filter:""};
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));
const fmt=v=>v?new Intl.DateTimeFormat("az-AZ",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}).format(new Date(v)):"—";

const statusLabels = {
  pending: "Növbədədir",
  generating: "Hazırlanır",
  rendering: "Render olunur",
  ready: "Upload hazırdır",
  uploading: "YouTube-a yüklənir",
  uploaded: "Yayımlanıb",
  failed: "Xəta",
  cancelled: "Ləğv edilib",
  active: "Aktiv",
  paused: "Dayandırılıb"
};

const badge = status => `
  <span class="badge ${esc(status)}">
    ${esc(statusLabels[status] || status || "Növbədədir")}
  </span>
`;

function toast(m){let e=$("#toast");e.textContent=m;e.className="show";clearTimeout(window.t);window.t=setTimeout(()=>e.className="",3000)}
function view(n){$$("aside nav button").forEach(x=>x.classList.toggle("active",x.dataset.view===n));$$(".view").forEach(x=>x.classList.toggle("active",x.id===`view-${n}`));$("#title").textContent={overview:"İcmal",channels:"Kanallar",videos:"Videolar",events:"Aktivlik"}[n];history.replaceState({},"",`#${n}`)}
async function load(){
 const [c,v,e]=await Promise.all([sb.from("channels").select("*").order("priority"),sb.from("video_queue").select("*,channel:channels(name)").order("created_at",{ascending:false}),sb.from("events").select("*").order("created_at",{ascending:false}).limit(100)]);
 if(c.error||v.error||e.error)throw(c.error||v.error||e.error);
 st.channels=c.data;st.videos=v.data;st.events=e.data;render()
}
function render(){
 $("#m1").textContent=st.channels.filter(x=>x.active&&x.autopilot).length;
 $("#m2").textContent=st.videos.filter(x=>!["uploaded","failed","cancelled"].includes(x.status)).length;
 $("#m3").textContent=st.videos.filter(x=>x.status==="ready").length;
 $("#m4").textContent=st.videos.filter(x=>x.status==="uploaded").length;
 $("#recent").className=st.videos.length?"":"empty";
 $("#recent").innerHTML=st.videos.slice(0,7).map(v=>`<div class="row"><span><b>${esc(v.title||v.topic||"Avtomatik mövzu")}</b><small>${esc(v.channel?.name||"Kanal")} · ${fmt(v.created_at)}</small></span>${badge(v.status)}</div>`).join("")||"Video yoxdur.";
 $("#rotation").className=st.channels.length?"":"empty";
 $("#rotation").innerHTML=st.channels.filter(x=>x.active&&x.autopilot).slice(0,7).map(c=>`<div class="row"><span><b>${esc(c.name)}</b><small>${esc(c.language)} · ${esc(c.niche)}</small></span>${badge("active")}</div>`).join("")||"Kanal yoxdur.";
 $("#channels").className=st.channels.length?"cards":"cards empty";
 $("#channels").innerHTML=st.channels.map(c=>`<article class="card"><div class="card-head"><div><h4>${esc(c.name)}</h4><small>${c.youtube_channel_id?"YouTube bağlıdır":"OAuth gözləyir"}</small></div>${badge(c.autopilot?"active":"paused")}</div><div class="meta"><div><span>Dil / mövzu</span><b>${esc(c.language)} · ${esc(c.niche)}</b></div><div><span>Həftəlik</span><b>${c.weekly_long_target} long · ${c.weekly_short_target} short</b></div><div><span>Long</span><b>${c.long_min_minutes}-${c.long_max_minutes} dəq</b></div><div><span>Səs</span><b>${esc(c.voice_provider)}</b></div></div><div class="card-actions"><button class="ghost" data-edit="${c.id}">Düzəliş</button><button class="primary" data-oauth="${c.id}">YouTube qoş</button><button class="ghost" data-toggle="${c.id}">Auto Pilot</button><button class="ghost" data-delete="${c.id}">Sil</button></div></article>`).join("")||"İlk kanalınızı əlavə edin.";
 renderVideos();
 $("#events").className=st.events.length?"video-list":"video-list empty";
 $("#events").innerHTML=st.events.map(e=>`<div class="video-item"><div><b>${esc(e.message)}</b><small>${esc(e.type)} · ${esc(e.level)}</small></div><time>${fmt(e.created_at)}</time></div>`).join("")||"Hadisə yoxdur.";
 $("#vChannel").innerHTML=st.channels.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join("");
}
function renderVideos(){
 let a=st.filter?st.videos.filter(v=>v.status===st.filter):st.videos;
 $("#videos").className=a.length?"video-list":"video-list empty";
 $("#videos").innerHTML=a.map(v=>`<div class="video-item"><div><b>${esc(v.title||v.topic||"Avtomatik mövzu")}</b><small>${esc(v.video_type)} · ${esc(v.id)}</small></div><span class="channel">${esc(v.channel?.name||"Kanal")}</span>${badge(v.status)}<time>${fmt(v.publish_at)}</time></div>`).join("")||"Video yoxdur.";
}
function openChannel(c={}){
 $("#channelForm").reset();$("#channelId").value=c.id||"";$("#channelDialogTitle").textContent=c.id?"Kanalı düzəlt":"Yeni kanal";
 const m={cName:c.name||"",cLanguage:c.language||"en",cNiche:c.niche||"motivation",cPrompt:c.custom_prompt||"",cAudience:c.audience_type||"general",cTimezone:c.timezone||"America/New_York",cLong:c.weekly_long_target??2,cShort:c.weekly_short_target??3,cLongMin:c.long_min_minutes??7,cLongMax:c.long_max_minutes??10,cShortMin:c.short_min_seconds??35,cShortMax:c.short_max_seconds??55,cVoiceProvider:c.voice_provider||"edge_tts",cVoiceId:c.voice_id||"en-US-AriaNeural",cPrivacy:c.privacy_status||"private",cPriority:c.priority??50};
 Object.entries(m).forEach(([k,v])=>$("#"+k).value=v);$("#cAuto").checked=c.autopilot??true;$("#channelDialog").showModal()
}
function payload(){return{name:$("#cName").value.trim(),language:$("#cLanguage").value,niche:$("#cNiche").value,custom_prompt:$("#cPrompt").value.trim()||null,audience_type:$("#cAudience").value,made_for_kids:$("#cAudience").value==="kids",timezone:$("#cTimezone").value,weekly_long_target:+$("#cLong").value,weekly_short_target:+$("#cShort").value,long_min_minutes:+$("#cLongMin").value,long_max_minutes:+$("#cLongMax").value,short_min_seconds:+$("#cShortMin").value,short_max_seconds:+$("#cShortMax").value,voice_provider:$("#cVoiceProvider").value,voice_id:$("#cVoiceId").value,privacy_status:$("#cPrivacy").value,priority:+$("#cPriority").value,autopilot:$("#cAuto").checked,active:true}}

async function connectYouTube(channelId) {
  const {
    data: { session },
    error: sessionError
  } = await sb.auth.getSession();

  if (sessionError) {
    throw sessionError;
  }

  if (!session?.access_token) {
    throw new Error(
      "Sessiya tapılmadı. Saytdan çıxıb yenidən daxil olun."
    );
  }

  const response = await fetch(
    `${URL}/functions/v1/automation`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${session.access_token}`,
        "apikey": KEY
      },

      body: JSON.stringify({
        action: "oauth_start",
        channel_id: channelId,
        return_url:
          "https://qerib-seferli.github.io/youtube-auto/"
      })
    }
  );

  const result = await response
    .json()
    .catch(() => ({}));

  if (!response.ok) {
    throw new Error(
      result.error ||
      `OAuth başladılmadı. HTTP status: ${response.status}`
    );
  }

  if (!result.url) {
    throw new Error(
      "Google OAuth ünvanı serverdən qaytarılmadı."
    );
  }

  window.location.assign(result.url);
}

async function init(){
 if("serviceWorker"in navigator)navigator.serviceWorker.register("./sw.js");
 const{data:{session}}=await sb.auth.getSession();$("#auth").classList.toggle("hidden",!!session);$("#app").classList.toggle("hidden",!session);if(session){view(location.hash.slice(1)||"overview");await load()}
 sb.auth.onAuthStateChange(async(_,s)=>{$("#auth").classList.toggle("hidden",!!s);$("#app").classList.toggle("hidden",!s);if(s)await load()});
 $("#authForm").onsubmit=async e=>{e.preventDefault();let{error}=await sb.auth.signInWithPassword({email:$("#email").value,password:$("#password").value});if(error)toast(error.message)};
 $("#signupBtn").onclick=async()=>{let{error}=await sb.auth.signUp({email:$("#email").value,password:$("#password").value});toast(error?error.message:"E-poçtu yoxlayın")};
 $("#logoutBtn").onclick=()=>sb.auth.signOut();$("#refreshBtn").onclick=()=>load().catch(e=>toast(e.message));$("#newChannelBtn").onclick=()=>openChannel();$("#newVideoBtn").onclick=()=>$("#videoDialog").showModal();$("#menuBtn").onclick=()=>$("#side").classList.toggle("open");
 $$("aside nav button").forEach(b=>b.onclick=()=>view(b.dataset.view));$$(".close").forEach(b=>b.onclick=()=>b.closest("dialog").close());$("#filter").onchange=e=>{st.filter=e.target.value;renderVideos()};
 $("#channelForm").onsubmit=async e=>{e.preventDefault();let id=$("#channelId").value,{data:{user}}=await sb.auth.getUser(),q=id?sb.from("channels").update(payload()).eq("id",id):sb.from("channels").insert({...payload(),owner_id:user.id}),{error}=await q;if(error)return toast(error.message);$("#channelDialog").close();await load()};
 $("#videoForm").onsubmit=async e=>{e.preventDefault();let{data:{user}}=await sb.auth.getUser(),raw=$("#vPublish").value,{error}=await sb.from("video_queue").insert({owner_id:user.id,channel_id:$("#vChannel").value,video_type:$("#vType").value,topic:$("#vTopic").value.trim()||null,publish_at:raw?new Date(raw).toISOString():null,status:"pending",source:"manual"});if(error)return toast(error.message);$("#videoDialog").close();await load();view("videos")};
 document.onclick=async e=>{let ed=e.target.closest("[data-edit]"),del=e.target.closest("[data-delete]"),tg=e.target.closest("[data-toggle]"),oa=e.target.closest("[data-oauth]");try{
  if(ed)openChannel(st.channels.find(x=>x.id===ed.dataset.edit));
  if(del&&confirm("Kanal silinsin?")){await sb.from("channels").delete().eq("id",del.dataset.delete);await load()}
  if(tg){let c=st.channels.find(x=>x.id===tg.dataset.toggle);await sb.from("channels").update({autopilot:!c.autopilot}).eq("id",c.id);await load()}

  if (oa) {
    await connectYouTube(
      oa.dataset.oauth
    );
  }
  
 }catch(x){toast(x.message)}};
 sb.channel("ui").on("postgres_changes",{event:"*",schema:"public",table:"video_queue"},()=>load()).subscribe()
}
init().catch(e=>toast(e.message));
