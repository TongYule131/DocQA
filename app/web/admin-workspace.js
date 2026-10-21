// 这里只负责 M1 页面分区，不作为账号或资料权限判断。
const view = document.body.dataset.workspaceView;
const isTools = view === 'tools';
const panels = [...document.querySelectorAll('[data-admin-panel]')];
function selectPanel(panel) {
  document.body.dataset.adminPanel = panel;
  for (const button of panels) button.setAttribute('aria-pressed', String(button.dataset.adminPanel === panel));
  document.getElementById('page-title').textContent = isTools ? '摘要与信息提取' : panel === 'diagnostics' ? '运行与连接' : '资料管理';
  document.getElementById('page-description').textContent = isTools
    ? '选择文档、查看处理范围，生成并复核带来源的摘要和结构化结果。'
    : panel === 'diagnostics' ? '查看本地服务的真实状态，按需检查模型连接。连接测试可能产生费用。'
      : '检查解析原文、质量告警和索引。模型连接与运行信息请前往系统设置。';
}
panels.forEach(button => button.onclick = () => selectPanel(button.dataset.adminPanel));
selectPanel('documents');
const question = document.getElementById('question');
question.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !question.disabled) {
    event.preventDefault(); document.getElementById('question-form').requestSubmit();
  }
});
