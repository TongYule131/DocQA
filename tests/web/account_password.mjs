// 账号安全表单的边界：重复点击、失败、条件、敏感值只在当前请求中使用。
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const source=(await readFile(new URL('../../app/web/account-password.js',import.meta.url),'utf8')).replace(/^import[^\n]+\n/,'');
async function boot(user,handler=async()=>({status:'password_changed'})) {
  const elements=Object.fromEntries(['old-password','new-password','confirm-password'].map(id=>[id,{value:''}])),calls=[],navigations=[];
  const document={getElementById(id){return elements[id]??=( {value:'',hidden:false,disabled:false,textContent:'',href:'',onsubmit:null});}};
  const context={document,window:{docqaAuthReady:Promise.resolve({user})},location:{assign:path=>navigations.push(path)},
    createApiClient:()=>async(path,options)=>{calls.push({path,options});return handler(path,options);}};
  await vm.runInNewContext('(async()=>{'+source+'})()',context);
  return {elements,calls,navigations,submit:()=>elements['password-form'].onsubmit({preventDefault(){}})};
}
const user={id:'synthetic',username:'alice',role:'customer',must_change_password:false};
const ordinary=await boot(user);assert.equal(ordinary.calls.length,0);assert.equal(ordinary.elements['security-back'].href,'/app');
ordinary.elements['old-password'].value='old';ordinary.elements['new-password'].value='new-one';ordinary.elements['confirm-password'].value='other';
await ordinary.submit();assert.equal(ordinary.calls.length,0);assert.equal(ordinary.elements.message.textContent,'两次新密码不一致');
ordinary.elements['new-password'].value=ordinary.elements['confirm-password'].value='old';await ordinary.submit();assert.equal(ordinary.calls.length,0);
console.log('PASS 页面加载零提交，确认不符和新旧相同在表单内拒绝');

let release;const gate=new Promise(resolve=>{release=resolve;});
const pending=await boot(user,()=>gate);
pending.elements['old-password'].value='old-secret';pending.elements['new-password'].value=pending.elements['confirm-password'].value='  new-secret  ';
const first=pending.submit();await pending.submit();assert.equal(pending.calls.length,1);assert.equal(pending.elements['save-password'].disabled,true);
assert.equal(JSON.parse(pending.calls[0].options.body).new_password,'  new-secret  ');
release({});await first;assert.deepEqual(pending.navigations,['/login?password_changed=1']);
assert.equal(pending.elements['new-password'].value,'');assert.equal(pending.elements['old-password'].value,'');
console.log('PASS 单次提交保留口令原字符，忙状态防重复，成功清空并重新登录');

const failed=await boot(user,async()=>{throw new Error('原密码不正确');});
failed.elements['old-password'].value='wrong';failed.elements['new-password'].value=failed.elements['confirm-password'].value='new-secret';
await failed.submit();assert.equal(failed.calls.length,1);assert.equal(failed.navigations.length,0);assert.equal(failed.elements['save-password'].disabled,false);assert.equal(failed.elements['old-password'].value,'');
assert.equal(failed.elements.message.textContent,'原密码不正确');
console.log('PASS 错误不重发、不误登出，恢复按钮并清空原密码');

const forced=await boot({...user,must_change_password:true});assert.equal(forced.elements['security-back'].hidden,true);assert.equal(forced.elements['security-heading'].textContent,'修改初始密码');
const admin=await boot({...user,role:'admin'});assert.equal(admin.elements['security-back'].href,'/admin');
const local=await boot(null);assert.equal(local.elements['password-form'].hidden,true);await local.submit();assert.equal(local.calls.length,0);
console.log('PASS 强制改密隐藏业务返回，管理员返回独立后台，本地无账号不执行改密');
