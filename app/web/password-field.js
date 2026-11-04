// 密码框的显示/隐藏切换：只改输入类型，不改 maxlength、autocomplete 与服务端校验。
export function installPasswordToggle(input, label = '显示密码') {
  if (!input || input.dataset.passwordToggle === 'ready') return null;
  input.dataset.passwordToggle = 'ready';
  const wrap = document.createElement('span'); wrap.className = 'password-field';
  input.before(wrap);
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'password-toggle secondary';
  button.textContent = label; button.setAttribute('aria-label', label); button.setAttribute('aria-pressed', 'false');
  button.onclick = () => {
    const shown = input.type === 'text';
    input.type = shown ? 'password' : 'text';
    button.textContent = shown ? label : '隐藏密码';
    button.setAttribute('aria-pressed', String(!shown));
    input.focus({ preventScroll: true });
  };
  wrap.append(input, button);
  return button;
}
