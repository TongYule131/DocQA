"""只读工具登记与参数边界；全部离线，不调用模型或业务服务。"""
import pytest
from fastapi import HTTPException
from app.knowledge_tools import validate_schema,validate_arguments,ToolRegistry


@pytest.mark.parametrize('schema',[
    {'type':'boolean','enum':[1]}, {'type':'integer','enum':[True]},
    {'type':'string','enum':[2]}, {'type':'string','minLength':2001},
    {'type':'array','minItems':101,'items':{'type':'string'}},
    {'type':'object','enum':[{'unknown':True}],'properties':{}},
])
def test_invalid_or_impossible_schema_is_rejected_before_parameter_extraction(schema):
    with pytest.raises(HTTPException) as error:validate_schema(schema)
    assert error.value.status_code==422


def test_boolean_does_not_equal_numeric_enum_and_large_integers_do_not_overflow():
    with pytest.raises(ValueError):validate_arguments(True,{'type':'boolean','enum':[1]})
    huge=10**1000
    validate_schema({'type':'integer','maximum':huge})
    assert validate_arguments(huge,{'type':'integer','maximum':huge})==huge
    assert validate_arguments(1,{'type':'number','enum':[1.0]})==1


@pytest.mark.parametrize('endpoint',['https://host.invalid:bad','http://host.invalid:70000','http://[broken'])
def test_invalid_url_is_rejected_before_registry_write(endpoint):
    class NoDatabase:
        def connect(self):raise AssertionError('invalid endpoint reached database')
    with pytest.raises(HTTPException) as error:
        ToolRegistry(NoDatabase()).save({'endpoint':endpoint,'read_only':True,'input_schema':{'type':'object'}})
    assert error.value.status_code==422
