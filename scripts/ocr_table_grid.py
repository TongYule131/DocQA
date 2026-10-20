"""按Docling TableData.grid语义还原稀疏空格；完全不读取gold。

缺少整个表、缺行/缺列、丢失非空内容仍不能通过。只在已声明的逻辑
行列范围内初始化空单元格，再按原始span覆盖；不补数字、不改行列。
"""
from __future__ import annotations
import copy


def canonical_table(table:dict)->dict:
    result=copy.deepcopy(table);rows,cols=table['rows'],table['cols']
    if type(rows) is not int or type(cols) is not int or not 0<rows<=500 or not 0<cols<=100:
        raise ValueError('表格声明尺寸无效')
    grid=[[{'r0':r,'r1':r+1,'c0':c,'c1':c+1,'text':'','column_header':False,'implicit_empty':True}
            for c in range(cols)] for r in range(rows)]
    for cell in table['cells']:
        if not 0<=cell['r0']<cell['r1']<=rows or not 0<=cell['c0']<cell['c1']<=cols:
            raise ValueError('单元格跨度超出声明尺寸')
        for r in range(cell['r0'],cell['r1']):
            for c in range(cell['c0'],cell['c1']):grid[r][c]=cell
    unique=[];seen=set()
    for row in grid:
        for cell in row:
            identity=id(cell)
            if identity not in seen:seen.add(identity);unique.append(copy.deepcopy(cell))
    result['cells']=unique
    result['implicit_empty_cells']=sum(c.get('implicit_empty',False) for c in unique)
    return result


def canonical_prediction(prediction:dict)->dict:
    result=copy.deepcopy(prediction)
    result['tables']=[canonical_table(t) for t in prediction['tables']]
    return result
