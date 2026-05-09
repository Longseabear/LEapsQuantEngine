from leaps_quant_engine.execution import ImmediateExecutionModel
from leaps_quant_engine.execution_model_loader import ExecutionModelLoadError, PythonExecutionModelLoader


def test_python_execution_model_loader_loads_class_reference():
    result = PythonExecutionModelLoader().load("leaps_quant_engine.execution:ImmediateExecutionModel")

    assert isinstance(result.model, ImmediateExecutionModel)
    assert result.model_name == "ImmediateExecutionModel"


def test_python_execution_model_loader_loads_file_factory(tmp_path):
    module_path = tmp_path / "execution.py"
    module_path.write_text(
        """
from leaps_quant_engine.execution import ImmediateExecutionModel


def create_execution_model(params):
    return ImmediateExecutionModel()
""".strip(),
        encoding="utf-8",
    )

    result = PythonExecutionModelLoader().load(module_path)

    assert isinstance(result.model, ImmediateExecutionModel)


def test_python_execution_model_loader_rejects_invalid_module(tmp_path):
    module_path = tmp_path / "execution.py"
    module_path.write_text("VALUE = 1", encoding="utf-8")

    try:
        PythonExecutionModelLoader().load(module_path)
    except ExecutionModelLoadError as exc:
        assert "create_execution_model" in str(exc)
    else:
        raise AssertionError("Expected ExecutionModelLoadError.")
