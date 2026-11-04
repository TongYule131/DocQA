import { createApiClient } from './api-client.js';
import { installPasswordToggle } from './password-field.js';
const api=createApiClient(),$=id=>document.getElementById(id),session=await window.docqaAuthReady;
let busy=false;
// 提示带类型：进行中为中性信息，失败才是红色，成功不再沿用失败配色。
const tell=(text,kind='info')=>{const target=$('message');target.textContent=text;target.dataset.kind=kind;};
for (const id of ['old-password','new-password','confirm-password']) installPasswordToggle($(id));
if (!session.user) { $('password-form').hidden=true; tell('请先使用团队账号登录。','error'); $('security-back').href='/login'; $('security-back').textContent='前往登录'; }
else {
  $('security-user').textContent=`账号：${session.user.username}`;
  $('security-back').href=session.user.role==='admin'?'/admin':'/app';
  if(session.user.must_change_password) {
    $('security-heading').textContent='修改初始密码'; $('security-hint').textContent='管理员已重置你的密码。请先设置不同的新密码，完成后重新登录，即可恢复业务访问。'; $('security-back').hidden=true;
  }
}
$('password-form').onsubmit=async event=>{
  event.preventDefault(); if(busy||!session.user)return;
  const oldPassword=$('old-password').value,newPassword=$('new-password').value,confirmPassword=$('confirm-password').value;
  if(newPassword!==confirmPassword){tell('两次新密码不一致','error');$('confirm-password').focus();return;}
  if(newPassword===oldPassword){tell('新密码不能与原密码相同','error');$('new-password').focus();return;}
  busy=true;$('save-password').disabled=true;tell('正在保存…','info');
  try {
    await api('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:oldPassword,new_password:newPassword,confirm_password:confirmPassword})});
    for(const id of ['old-password','new-password','confirm-password'])$(id).value='';
    location.assign('/login?password_changed=1');
  } catch(error){tell(error.message,'error');$('old-password').value='';$('old-password').focus();}
  finally{busy=false;$('save-password').disabled=false;}
};
