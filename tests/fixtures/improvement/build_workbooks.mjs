// 一次性或显式重建的离线制版器；默认准备命令不运行 Node 或供应商。
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';
const base = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const payload = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
for (const source of payload) {
  const wb = Workbook.create();
  const data = wb.worksheets.add(source.id === 'xlsx-stock' ? '盘点' : '经营');
  const status = wb.worksheets.add(source.id === 'xlsx-stock' ? '流水状态' : '预算状态');
  const note = wb.worksheets.add('说明');
  data.getRange('A1:F1').merge();
  data.getRange('A1').values = [[source.title]];
  data.getRange('A2:F2').merge();
  data.getRange('A2').values = [['合成数据 CC0-1.0 全部内容虚构 只用于工程评估']];
  const matrices = {
    'xlsx-revenue': [
      ['指标', '金额', null, '服务量', null, '口径'],
      [null, '2024万元', '2025万元', '2024人次', '2025人次', null],
      ['营业收入', 1100, 1280, null, null, '2025未经审计'],
      ['营业成本', null, 860, null, null, '2025未经审计'],
      ['服务量', null, null, 3000, 3200, '重复到访按人次'],
      ['补助收入', null, null, null, null, '未填报不是零'],
      ['退款金额', null, 0, null, null, '已核实为零'],
      ['收入成本差额', null, null, null, null, '公式缓存刻意缺失']],
    'xlsx-energy': [
      ['分区', '2025Q3电量', null, '2025Q3用水', null, '口径'],
      [null, '购电千瓦时', '返送千瓦时', '立方米', '状态', null],
      ['甲区', 18000, null, 120, '已核', '含公共照明'],
      ['乙区', 18000, 0, null, '用水缺测', '水量不含施工取水'],
      ['两区合计', null, null, null, '不重算', '公式缓存刻意缺失']],
    'xlsx-stock': [
      ['盘点日2025-12-31', '数量件', null, '复核', null, '范围'],
      ['物料', '账面', '实盘', '状态', '原因', '北区'],
      ['A', 80, 78, '待核', '责任未裁定', '不推断失窃'],
      ['B', 0, 0, '已核', '零为真实值', '数量复核'],
      ['C', 15, null, '缺测', '尚未实盘', '空白不是零'],
      ['差异汇总', null, null, '不重算', '公式缓存刻意缺失', null]],
  };
  const rows = matrices[source.id];
  data.getRange(`A3:F${rows.length + 2}`).values = rows;
  data.getRange('B3:C3').merge();
  data.getRange('D3:E3').merge();
  const formulas = {'xlsx-revenue':['C10','=C5-C6'], 'xlsx-energy':['B7','=SUM(B5:B6)'], 'xlsx-stock':['B8','=C5-B5']};
  const [cell, formula] = formulas[source.id];
  data.getRange(cell).formulas = [[formula]];
  status.getRange('A1:C1').values = [['字段', '数值或状态', '用途']];
  status.getRange('A2:C5').values = source.id === 'xlsx-stock'
    ? [['正式收发', 0, '与盘点分开'], ['模拟发出', 10, '演练不计入正式'], ['期后更正', '未批准', '不能改写年末'], ['已核', '数量复核', '不代表财务审计']]
    : source.id === 'xlsx-energy'
      ? [['测试负荷千瓦', 300, '过程输入不是千瓦时'], ['甲区', '已核', '购电含公共照明'], ['乙区用水', '缺测', '水表更换'], ['历史2024', '未提供', '不能补零']]
      : [['收入预算万元', 1500, '预算不是实际'], ['2025审计', '未经审计', '口径随事实'], ['补助', '未填报', '空白不是零'], ['2026实际', '未提供', '不能推算']];
  note.getRange('A1:B1').values = [['材料说明', '全部机构与数字为合成 CC0-1.0']];
  note.getRange('A3:B3').values = [['锚点', '完整范围说明 原文']];
  note.getRange('A4:B13').values = source.quotes.map((text, i) => [`F${String(i + 1).padStart(2, '0')}`, text]);
  for (const sheet of [data, status, note]) {
    sheet.showGridLines = false;
    sheet.freezePanes.freezeRows(4);
    sheet.getUsedRange().format.font.name = 'Microsoft YaHei';
    sheet.getUsedRange().format.font.size = 11;
    sheet.getUsedRange().format.wrapText = true;
    sheet.getUsedRange().format.rowHeight = sheet === note ? 65 : 42;
    sheet.getUsedRange().format.columnWidth = 20;
  }
  note.getRange('A:A').format.columnWidth = 12;
  note.getRange('B:B').format.columnWidth = 105;
  data.getRange('A:A').format.columnWidth = 28;
  data.getRange('F:F').format.columnWidth = 30;
  wb.recalculate();
  const output = await SpreadsheetFile.exportXlsx(wb);
  await output.save(path.join(base, 'assets', `${source.id}.xlsx`));
  for (const [sheetName, range, label] of [[data.name,'A1:F11','data'],[status.name,'A1:C5','status'],['说明','A1:B13','note']]) {
    const preview = await wb.render({sheetName, range, scale: 1, format: 'png'});
    await fs.writeFile(path.join(path.dirname(process.argv[2]), `${source.id}-${label}.png`), new Uint8Array(await preview.arrayBuffer()));
  }
}
