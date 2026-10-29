// 实际浮层控制器：互斥、外部点击和重绘后内部点击；不接业务服务。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=(await readFile(new URL('../../app/web/chat-shell.js',import.meta.url),'utf8')).replace(/^import .*;\r?\n/gm,'');
const menu=()=>({open:false,listeners:{},contains(target){return target===this;},addEventListener(type,handler){this.listeners[type]=handler;}});
const workspace=menu(),settings=menu(),scope=menu(),menus=[workspace,settings,scope],events={};
const document={querySelector(){return null;},querySelectorAll(selector){return selector.includes('#scope-filter')?menus:[];},addEventListener(type,handler){events[type]=handler;}};
vm.runInNewContext(source,{document,window:{},console});
settings.open=true;scope.open=true;scope.listeners.toggle();
assert.equal(settings.open,false,'资料面板打开时关闭会话设置');
assert.equal(scope.open,true);
const removedButton={};events.click({target:removedButton,composedPath:()=>[removedButton,scope]});
assert.equal(scope.open,true,'全选/清空重绘不能被误认成外部点击');
settings.open=true;settings.listeners.toggle();assert.equal(scope.open,false);
events.click({target:{},composedPath:()=>[]});assert(menus.every(item=>!item.open));
workspace.open=true;events.keydown({key:'Escape'});assert(menus.every(item=>!item.open));
console.log('PASS chat shell: mutually exclusive overlays, internal redraw clicks, outside close and Escape');
