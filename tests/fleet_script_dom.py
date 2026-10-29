"""Small DOM model for exercising shipped filtering scripts without a browser."""

import json
import shutil
import subprocess
from html.parser import HTMLParser


class Tree(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"input", "br", "meta", "link", "hr", "img", "source", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) > 1 and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


DOM = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(raw,parent=null){this.tag=raw.tag;this.attrs={...raw.attrs};this.parent=parent;this.handlers={};
 this.children=raw.children.map(child=>typeof child==='string'?child:new Element(child,this));
 this.hidden='hidden' in this.attrs;this.disabled='disabled' in this.attrs;this.value=this.attrs.value||'';
 this.classList={add:name=>{this.attrs.class=(this.attrs.class||'')+' '+name;}};}
 get id(){return this.attrs.id;}
 get dataset(){let result={};Object.entries(this.attrs).filter(([k])=>k.startsWith('data-')).forEach(([k,v])=>{
 result[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;});return result;}
 get textContent(){return this.children.map(child=>typeof child==='string'?child:child.textContent).join('');}
 set textContent(value){this.children=[value];}
 setAttribute(key,value){this.attrs[key]=value;}
 removeAttribute(key){delete this.attrs[key];}
 hasAttribute(key){return key in this.attrs;}
 getAttribute(key){return key in this.attrs?this.attrs[key]:null;}
 addEventListener(event,handler){this.handlers[event]=handler;}
 matches(selector){
 const parts=selector.split(/\s+/);if(parts.length>1){if(!this.matches(parts.pop()))return false;
 let parent=this.parent;while(parts.length){const expected=parts.pop();
 while(parent&&!parent.matches(expected))parent=parent.parent;if(!parent)return false;parent=parent.parent;}return true;}
 if(selector.startsWith('.'))return (this.attrs.class||'').split(/\s+/).includes(selector.slice(1));
 if(selector.startsWith('['))return selector.slice(1,-1) in this.attrs;return this.tag===selector;}
 querySelectorAll(selector){let result=[];const choices=selector.split(',').map(s=>s.trim());
 function walk(node){for(const child of node.children){if(typeof child==='string')continue;
 if(choices.some(s=>child.matches(s)))result.push(child);walk(child);}}walk(this);return result;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 cloneNode(){return new Element(JSON.parse(JSON.stringify(this.raw())));}
 raw(){return {tag:this.tag,attrs:this.attrs,children:this.children.map(c=>typeof c==='string'?c:c.raw())};}
 remove(){this.parent.children=this.parent.children.filter(c=>c!==this);}
}
const dom=new Element(TREE);
const htmlRoot=new Element({tag:'html',attrs:{},children:[]});
const store={};
global.localStorage={getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);}};
global.document={readyState:'complete',documentElement:htmlRoot,querySelectorAll:s=>dom.querySelectorAll(s),querySelector:s=>dom.querySelector(s)};
global.window={addEventListener(){}};
global.location={hash:''};global.history={replaceState(){}};
"""


def run_script(page, script, assertions):
    node = shutil.which("node")
    assert node, "Installed Node must be on PATH; filtering tests must not skip"
    program = DOM.replace("TREE", json.dumps(Tree(page).root)) + "\neval(" + json.dumps(script) + ");\n" + assertions
    result = subprocess.run([node, "-e", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
