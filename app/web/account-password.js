import { createApiClient } from './api-client.js';
const api=createApiClient(),$=id=>document.getElementById(id),session=await window.docqaAuthReady;
let busy=false;
if (!session.user) { $('password-form').hidden=true; $('message').textContent='请先使用团队账号登录。'; $('security-back').href='/login'; $('security-back').textContent='前往登录'; }
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
  if(newPassword!==confirmPassword){$('message').textContent='两次新密码不一致';return;}
  if(newPassword===oldPassword){$('message').textContent='新密码不能与原密码相同';return;}
  busy=true;$('save-password').disabled=true;$('message').textContent='正在保存…';
  try {
    await api('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:oldPassword,new_password:newPassword,confirm_password:confirmPassword})});
    for(const id of ['old-password','new-password','confirm-password'])$(id).value='';
    location.assign('/login?password_changed=1');
  } catch(error){$('message').textContent=error.message;$('old-password').value='';}
  finally{busy=false;$('save-password').disabled=false;}
};
