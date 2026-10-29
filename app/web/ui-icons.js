// 固定 SVG 路径构建界面图标，不把文档或模型文本解释为 SVG/HTML。
const paths = {
  panel: ['M9 3v18','M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z'],
  book: ['M4 4h6a3 3 0 0 1 2 1 3 3 0 0 1 2-1h6v15h-6a3 3 0 0 0-2 1 3 3 0 0 0-2-1H4Z','M12 5v15'],
  layers: ['m12 3 9 5-9 5-9-5 9-5Z','m3 12 9 5 9-5','m3 16 9 5 9-5'],
  tree: ['M6 5v13h12V8','M6 12h12','M4 3h4v4H4Z','M16 6h4v4h-4Z','M16 16h4v4h-4Z'],
  list: ['M8 5h12M8 12h12M8 19h12','M3 5h1M3 12h1M3 19h1'],
  trace: ['m3 12 4-7 4 14 4-10 3 3h3'],
  users: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2','M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8','M17 3a4 4 0 0 1 0 8','M22 21v-2a4 4 0 0 0-3-3.9'],
  settings: ['m12 3 2 3 3-.2 1.5 2.6-1.5 2.6 1.5 2.6-1.5 2.6-3-.2-2 3-2-3-3 .2L5.5 14 7 11.4 5.5 8.8 7 6.2 10 6Z','M12 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6'],
  chat: ['M21 11a8 8 0 0 1-8 8H7l-4 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z'],
  feedback: ['M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z','M7 9h10M7 13h7'],
  chevron: ['m8 10 4 4 4-4'],
  more: ['M5 12h.01M12 12h.01M19 12h.01'],
  plus: ['M12 5v14M5 12h14'],
  send: ['M12 19V5','m5 12 7-7 7 7'],
  refresh: ['M20 7v5h-5','M4 17v-5h5','M6 7a7 7 0 0 1 12-1l2 6','M18 17a7 7 0 0 1-12 1l-2-6'],
};
export function icon(name) {
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
  svg.setAttribute('width','18');svg.setAttribute('height','18');svg.setAttribute('focusable','false');
  svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.7');svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');svg.setAttribute('aria-hidden','true');svg.classList.add('ui-icon');
  for (const d of paths[name] || paths.more) { const p=document.createElementNS(ns,'path');p.setAttribute('d',d);svg.append(p); }
  return svg;
}
