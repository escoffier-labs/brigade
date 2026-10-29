"""Two-column page shell for the Command Deck pages.

``deck_page`` wraps an already-escaped sidebar and body in the shared
``ui_theme.document`` and attaches the machine filter script. The script is a
constant: it builds the filter input with DOM calls, reads ``textContent`` and
the input value only, and never writes markup. Without JavaScript the input is
simply absent and the machine tree stays fully visible.
"""

from __future__ import annotations

import html

from . import ui_theme

FILTER_SCRIPT = (
    "(function () {"
    "function start() {"
    "var host = document.querySelector('[data-tree-filter]');"
    "if (!host || host.querySelector('input')) { return; }"
    "var input = document.createElement('input');"
    "input.type = 'search';"
    "input.placeholder = 'Filter machines';"
    "input.setAttribute('aria-label', 'Filter machines and cloud providers');"
    "host.appendChild(input);"
    "function apply() {"
    "var query = input.value.toLowerCase();"
    "document.querySelectorAll('[data-filter-row]').forEach(function (row) {"
    "row.hidden = query !== '' && (row.textContent || '').toLowerCase().indexOf(query) === -1;"
    "});}"
    "try { input.value = sessionStorage.getItem('deck-filter') || ''; } catch (error) { input.value = ''; }"
    "input.addEventListener('input', function () {"
    "try { sessionStorage.setItem('deck-filter', input.value); } catch (error) { /* storage blocked */ }"
    "apply();});"
    "input.addEventListener('focus', function () {"
    "try { sessionStorage.setItem('deck-filter-focus', '1'); } catch (error) { /* storage blocked */ }"
    "});"
    "input.addEventListener('blur', function () {"
    "try { sessionStorage.removeItem('deck-filter-focus'); } catch (error) { /* storage blocked */ }"
    "});"
    "apply();"
    "try {"
    "if (sessionStorage.getItem('deck-filter-focus') === '1') {"
    "input.focus();"
    "input.setSelectionRange(input.value.length, input.value.length);"
    "}"
    "} catch (error) { /* storage blocked */ }"
    "}"
    "(typeof window !== 'undefined' && window.fleetOnInit || function (f) { if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', f); } else { f(); } })(start);"
    "})();"
)


# Registers page initializers so a soft refresh can re-run them on swapped markup.
INIT_SCRIPT = (
    "(function(){var fns=[];"
    "window.fleetOnInit=function(fn){fns.push(fn);"
    "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',fn);}else{fn();}};"
    "window.fleetReinit=function(){fns.forEach(function(fn){try{fn();}catch(error){}});};"
    "})();"
)

# In-place refresh. Nothing from a fetched page is executed: it is parsed with
# DOMParser (inert), every <script> is dropped, and only the sidebar and the
# content below the section tabs are replaced. ``__SECONDS__`` is substituted
# with an integer, never with page data.
SOFT_REFRESH_SCRIPT = """(function(){
var SECONDS=__SECONDS__,timer=null,dirty=false,stopped=false,busy=false,missed=false;
document.addEventListener('input',function(e){
if(!e.isTrusted)return;var t=e.target;
if(t&&t.closest&&t.closest('form')&&t.type!=='search')dirty=true;},true);
function typing(){var a=document.activeElement;
return !!a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT'||a.isContentEditable===true);}
function selecting(){var s=window.getSelection&&window.getSelection();return !!s&&!s.isCollapsed&&String(s).length>0;}
function schedule(){if(stopped)return;clearTimeout(timer);timer=setTimeout(tick,SECONDS*1000);}
function tick(){
if(stopped)return;
if(document.hidden){missed=true;return;}
if(busy||dirty||typing()||selecting()){schedule();return;}
refresh();}
function regionOf(main){var kids=Array.prototype.slice.call(main.children),at=-1;
kids.forEach(function(k,i){if(k.classList.contains('section-tabs'))at=i;});
if(at<0)kids.forEach(function(k,i){if(k.classList.contains('admin-section-head'))at=i;});
return kids.slice(at+1);}
function pathOf(el,root){var out=[];
while(el&&el!==root){out.unshift(Array.prototype.indexOf.call(el.parentNode.children,el));el=el.parentNode;}
return el===root?out:null;}
function resolve(root,path){var el=root;
for(var i=0;i<path.length;i++){el=el&&el.children[path[i]];}return el||null;}
function inRegion(el,main){var top=el;while(top.parentNode&&top.parentNode!==main)top=top.parentNode;
return !(top.classList&&(top.classList.contains('section-tabs')||top.classList.contains('admin-section-head')));}
function capture(side,main){
var st={x:window.scrollX,y:window.scrollY,scrolls:[],details:[],controls:[],pressed:[],focus:null,roots:[side,main]};
[side,main].forEach(function(root,ri){
if(root.scrollTop||root.scrollLeft)st.scrolls.push({r:ri,p:[],t:root.scrollTop,l:root.scrollLeft});
Array.prototype.forEach.call(root.querySelectorAll('*'),function(el){
if(ri===1&&!inRegion(el,main))return;
var p=pathOf(el,root);if(!p)return;
if(el.scrollTop||el.scrollLeft)st.scrolls.push({r:ri,p:p,t:el.scrollTop,l:el.scrollLeft});
if(el.tagName==='DETAILS')st.details.push({r:ri,p:p,open:el.open});
if(ri===1&&el.hasAttribute('aria-pressed'))st.pressed.push({p:p,v:el.getAttribute('aria-pressed')});
if(ri===1&&(el.tagName==='INPUT'||el.tagName==='SELECT'||el.tagName==='TEXTAREA')){
var type=(el.type||'').toLowerCase();
if(['hidden','password','file','submit','button','reset','image'].indexOf(type)!==-1)return;
var changed;
if(el.tagName==='SELECT'){changed=!Array.prototype.every.call(el.options,function(o){return o.selected===o.defaultSelected;});}
else if(type==='checkbox'||type==='radio'){changed=el.checked!==el.defaultChecked;}
else{changed=el.value!==el.defaultValue;}
if(changed)st.controls.push({p:p,tag:el.tagName,type:type,name:el.name,value:el.value,checked:el.checked});}});});
var a=document.activeElement;
if(a&&a!==document.body){[side,main].forEach(function(root,ri){var p=root.contains(a)&&a!==root?pathOf(a,root):null;
if(p&&(ri===0||inRegion(a,main)))st.focus={r:ri,p:p};});}
return st;}
function fire(el,name){el.dispatchEvent(new Event(name,{bubbles:true}));}
function restore(st,side,main){
var roots=[side,main];
st.controls.forEach(function(c){var el=resolve(main,c.p);
if(!el||el.tagName!==c.tag||(el.type||'').toLowerCase()!==c.type||el.name!==c.name)return;
if(c.type==='checkbox'||c.type==='radio'){el.checked=c.checked;}else{el.value=c.value;}
fire(el,'input');fire(el,'change');});
st.pressed.forEach(function(b){var el=resolve(main,b.p);
if(el&&el.getAttribute('aria-pressed')!==b.v&&b.v==='true'&&el.click)el.click();});
st.details.forEach(function(d){var el=resolve(roots[d.r],d.p);if(el&&el.tagName==='DETAILS')el.open=d.open;});
st.scrolls.forEach(function(s){var el=resolve(roots[s.r],s.p);if(el){el.scrollTop=s.t;el.scrollLeft=s.l;}});
window.scrollTo(st.x,st.y);
if(st.focus){var f=resolve(roots[st.focus.r],st.focus.p);if(f&&f.focus){try{f.focus({preventScroll:true});}catch(error){}}}}
function strip(node){Array.prototype.forEach.call(node.querySelectorAll('script'),function(s){s.remove();});
return node;}
function swap(text){
var next=new DOMParser().parseFromString(text,'text/html');
var side=document.querySelector('.shell-sidebar'),main=document.querySelector('.shell-main');
var nside=next.querySelector('.shell-sidebar'),nmain=next.querySelector('.shell-main');
if(!side||!main||!nside||!nmain)return;
var st=capture(side,main);
var old=regionOf(main),fresh=regionOf(nmain);
old.forEach(function(n){n.remove();});
fresh.forEach(function(n){if(n.tagName==='SCRIPT')return;main.appendChild(strip(document.importNode(n,true)));});
side.textContent='';
Array.prototype.forEach.call(nside.children,function(n){if(n.tagName==='SCRIPT')return;side.appendChild(strip(document.importNode(n,true)));});
if(next.title)document.title=next.title;
var stamp=next.body&&next.body.getAttribute('data-as-of');
if(stamp!==null&&stamp!==undefined)document.body.setAttribute('data-as-of',stamp);
if(window.fleetReinit)window.fleetReinit();
restore(st,side,main);}
function refresh(){
busy=true;var where=location.pathname+location.search;
fetch(location.href,{credentials:'same-origin',cache:'no-store',redirect:'manual',headers:{'X-Fleet-Soft-Refresh':'1'}}).then(function(res){
if(res.type==='opaqueredirect'||res.status===401||res.status===403){stopped=true;return null;}
var kind=res.headers.get('content-type')||'';
if(res.status!==200||kind.indexOf('text/html')!==0)return null;
return res.text();
}).then(function(text){
if(text===null||stopped||where!==location.pathname+location.search)return;
if(dirty||typing()||selecting())return;
swap(text);
}).catch(function(){}).then(function(){busy=false;schedule();});}
document.addEventListener('visibilitychange',function(){
if(!document.hidden&&missed&&!stopped){missed=false;tick();}});
schedule();
})();"""


def soft_refresh_script(seconds: int) -> str:
    return SOFT_REFRESH_SCRIPT.replace("__SECONDS__", str(int(seconds)))


def deck_page(
    title: str,
    active: str,
    sidebar_html: str,
    body_html: str,
    nonce: str,
    *,
    as_of: str,
    refresh_seconds: int | None = 10,
    extra_css: str = "",
    script: str = "",
    title_text: str | None = None,
) -> str:
    """The shared shell. *sidebar_html* and *body_html* must already be escaped.

    A page with *refresh_seconds* refreshes in place through
    ``SOFT_REFRESH_SCRIPT``; browsers without scripts keep a ``<noscript>``
    meta refresh. *title_text* overrides *title* for the ``<title>`` element.
    """
    shell = (
        f'<div class="shell" data-active="{html.escape(active, quote=True)}">'
        f"{sidebar_html}"
        f'<div class="shell-main">{body_html}</div></div>'
    )
    soft = soft_refresh_script(refresh_seconds) if refresh_seconds else ""
    return ui_theme.document(
        title if title_text is None else title_text,
        nonce,
        shell,
        as_of=as_of,
        refresh_seconds=refresh_seconds,
        soft_refresh=bool(refresh_seconds),
        extra_css=extra_css,
        script=INIT_SCRIPT + soft + FILTER_SCRIPT + script,
    )
